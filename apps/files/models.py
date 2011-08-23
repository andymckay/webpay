from datetime import datetime, timedelta
import hashlib
import json
import os
import posixpath
import re
import unicodedata
import uuid
import shutil
import time
import zipfile

import django.dispatch
from django.conf import settings
from django.db import models
from django.dispatch import receiver
from django.template.defaultfilters import slugify
from django.utils.encoding import smart_str

import commonware
import path
from statsd import statsd
from uuidfield.fields import UUIDField
import waffle

import amo
import amo.models
import amo.utils
from amo.urlresolvers import reverse
from applications.models import Application, AppVersion
from apps.amo.utils import memoize
import devhub.signals
from files.utils import RDF, SafeUnzip
from versions.compare import version_int as vint

log = commonware.log.getLogger('z.files')

# Acceptable extensions.
EXTENSIONS = ('.xpi', '.jar', '.xml', '.webapp', '.json')


class File(amo.models.OnChangeMixin, amo.models.ModelBase):
    STATUS_CHOICES = amo.STATUS_CHOICES.items()

    version = models.ForeignKey('versions.Version', related_name='files')
    platform = models.ForeignKey('Platform', default=amo.PLATFORM_ALL.id)
    filename = models.CharField(max_length=255, default='')
    size = models.PositiveIntegerField(default=0)  # kilobytes
    hash = models.CharField(max_length=255, default='')
    # TODO: delete this column
    codereview = models.BooleanField(default=False)
    jetpack_version = models.CharField(max_length=10, null=True)
    status = models.PositiveSmallIntegerField(choices=STATUS_CHOICES,
                                              default=amo.STATUS_UNREVIEWED)
    datestatuschanged = models.DateTimeField(null=True, auto_now_add=True)
    no_restart = models.BooleanField(default=False)

    reviewed = models.DateTimeField(null=True)

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'files'

    def __unicode__(self):
        return unicode(self.id)

    @property
    def amo_platform(self):
        # TODO: Ideally this would be ``platform``.
        return amo.PLATFORMS[self.platform_id]

    @property
    def has_been_validated(self):
        try:
            self.validation
        except FileValidation.DoesNotExist:
            return False
        else:
            return True

    def get_mirror(self, addon, attachment=False):
        if self.datestatuschanged:
            published = datetime.now() - self.datestatuschanged
        else:
            published = timedelta(minutes=0)

        if attachment:
            host = posixpath.join(settings.LOCAL_MIRROR_URL, '_attachments')
        elif addon.is_disabled or self.status == amo.STATUS_DISABLED:
            host = settings.PRIVATE_MIRROR_URL
        elif (addon.status == amo.STATUS_PUBLIC
              and not addon.disabled_by_user
              and self.status in (amo.STATUS_PUBLIC, amo.STATUS_BETA)
              and published > timedelta(minutes=settings.MIRROR_DELAY)
              and not settings.DEBUG):
            host = settings.MIRROR_URL  # Send it to the mirrors.
        else:
            host = settings.LOCAL_MIRROR_URL

        return posixpath.join(*map(smart_str, [host, addon.id, self.filename]))

    def get_url_path(self, src):
        from amo.helpers import urlparams, absolutify
        url = os.path.join(reverse('downloads.file', args=[self.id]),
                           self.filename)
        # Firefox's Add-on Manager needs absolute urls.
        return absolutify(urlparams(url, src=src))

    @classmethod
    def from_upload(cls, upload, version, platform, parse_data={}):
        f = cls(version=version, platform=platform)
        upload.path = path.path(nfd_str(upload.path))
        f.filename = f.generate_filename(extension=upload.path.ext or '.xpi')
        f.size = int(max(1, round(upload.path.size / 1024, 0)))  # Kilobytes.
        f.jetpack_version = cls.get_jetpack_version(upload.path)
        f.no_restart = parse_data.get('no_restart', False)
        if version.addon.status == amo.STATUS_PUBLIC:
            if amo.VERSION_BETA.search(parse_data.get('version', '')):
                f.status = amo.STATUS_BETA
            elif version.addon.trusted:
                f.status = amo.STATUS_PUBLIC
        elif (version.addon.status in amo.LITE_STATUSES
              and version.addon.trusted):
            f.status = version.addon.status
        elif version.addon.is_webapp():
            # Files don't really matter for webapps, just make them public.
            f.status = amo.STATUS_PUBLIC
        f.hash = (f.generate_hash(upload.path)
                  if waffle.switch_is_active('file-hash-paranoia')
                  else upload.hash)
        f.save()
        log.debug('New file: %r from %r' % (f, upload))
        # Move the uploaded file from the temp location.
        destinations = [path.path(version.path_prefix)]
        if f.status in amo.MIRROR_STATUSES:
            destinations.append(path.path(version.mirror_path_prefix))
        for dest in destinations:
            if not dest.exists():
                dest.makedirs()
            upload.path.copyfile(dest / nfd_str(f.filename))
        if upload.validation:
            FileValidation.from_json(f, upload.validation)
        return f

    @classmethod
    def get_jetpack_version(cls, path):
        try:
            zip_, name = zipfile.ZipFile(path), 'harness-options.json'
            if name in zip_.namelist():
                opts = json.load(zip_.open(name))
                return opts['sdkVersion']
        except Exception:
            return None

    def generate_hash(self, filename=None):
        """Generate a hash for a file."""
        hash = hashlib.sha256()
        with open(filename if filename else self.file_path, 'rb') as obj:
            for chunk in iter(lambda: obj.read(1024), ''):
                hash.update(chunk)
        return 'sha256:%s' % hash.hexdigest()

    def generate_filename(self, extension='.xpi'):
        """
        Files are in the format of:
        {addon_name}-{version}-{apps}-{platform}
        """
        parts = []
        # slugify drops unicode so we may end up with an empty string.
        # Apache did not like serving unicode filenames (bug 626587).
        name = slugify(self.version.addon.name).replace('-', '_') or 'addon'
        parts.append(name)
        parts.append(self.version.version)

        if self.version.compatible_apps:
            apps = '+'.join([a.shortername for a in
                             self.version.compatible_apps])
            parts.append(apps)

        if self.platform_id and self.platform_id != amo.PLATFORM_ALL.id:
            parts.append(amo.PLATFORMS[self.platform_id].shortname)

        self.filename = '-'.join(parts) + extension
        return self.filename

    _pretty_filename = re.compile(r'(?P<slug>[a-z0-7_]+)(?P<suffix>.*)')

    def pretty_filename(self, maxlen=20):
        """Displayable filename.

        Truncates filename so that the slug part fits maxlen.
        """
        m = self._pretty_filename.match(self.filename)
        if not m:
            return self.filename
        if len(m.group('slug')) < maxlen:
            return self.filename
        return u'%s...%s' % (m.group('slug')[0:(maxlen - 3)],
                             m.group('suffix'))

    def latest_xpi_url(self):
        addon = self.version.addon_id
        kw = {'addon_id': addon}
        if self.platform_id != amo.PLATFORM_ALL.id:
            kw['platform'] = self.platform_id
        url = reverse('downloads.latest', kwargs=kw)
        return os.path.join(url, 'addon-%s-latest%s' % (addon, self.extension))

    def eula_url(self):
        return reverse('addons.eula', args=[self.version.addon_id, self.id])

    @property
    def file_path(self):
        return os.path.join(settings.ADDONS_PATH, str(self.version.addon_id),
                            self.filename)

    @property
    def mirror_file_path(self):
        return os.path.join(settings.MIRROR_STAGE_PATH,
                            str(self.version.addon_id), self.filename)

    @property
    def guarded_file_path(self):
        return os.path.join(settings.GUARDED_ADDONS_PATH,
                            str(self.version.addon_id), self.filename)

    def watermarked_file_path(self, user_pk):
        return os.path.join(settings.WATERMARKED_ADDONS_PATH,
                            str(self.version.addon_id),
                            '%s-%s-%s' % (self.pk, user_pk, self.filename))

    @property
    def extension(self):
        return os.path.splitext(self.filename)[-1]

    @classmethod
    def mv(cls, src, dst, msg):
        """Move a file from src to dst."""
        try:
            src, dst = path.path(src), path.path(dst)
            if src.exists():
                log.info(msg % (src, dst))
                if not dst.dirname().exists():
                    dst.dirname().makedirs()
                src.rename(dst)
        except UnicodeEncodeError:
            log.error('Move Failure: %s %s' % (smart_str(src), smart_str(dst)))

    def hide_disabled_file(self):
        """Move a disabled file to the guarded file path."""
        if not self.filename:
            return
        src, dst = self.file_path, self.guarded_file_path
        self.mv(src, dst, 'Moving disabled file: %s => %s')
        # Remove the file from the mirrors if necessary.
        if os.path.exists(smart_str(self.mirror_file_path)):
            log.info('Unmirroring disabled file: %s'
                     % self.mirror_file_path)
            os.remove(self.mirror_file_path)

    def unhide_disabled_file(self):
        if not self.filename:
            return
        src, dst = self.guarded_file_path, self.file_path
        self.mv(src, dst, 'Moving undisabled file: %s => %s')

    def copy_to_mirror(self):
        if not self.filename:
            return
        try:
            if os.path.exists(self.file_path):
                dst = self.mirror_file_path
                log.info('Moving file to mirror: %s => %s'
                         % (self.file_path, dst))
                if not os.path.exists(os.path.dirname(dst)):
                    os.makedirs(os.path.dirname(dst))
                shutil.copyfile(self.file_path, dst)
        except UnicodeEncodeError:
            log.info('Copy Failure: %s %s %s' %
                     (self.id, smart_str(self.filename),
                      smart_str(self.file_path)))

    _get_localepicker = re.compile('^locale browser ([\w\-_]+) (.*)$', re.M)

    @memoize(prefix='localepicker', time=0)
    def get_localepicker(self):
        """
        For a file that is part of a language pack, extract
        the chrome/localepicker.properties file and return as
        a string.
        """
        start = time.time()
        zip = SafeUnzip(self.file_path)
        if not zip.is_valid(fatal=False):
            return ''

        try:
            manifest = zip.extract_path('chrome.manifest')
        except KeyError, e:
            log.info('No file named: chrome.manifest in file: %s' % self.pk)
            return ''

        res = self._get_localepicker.search(manifest)
        if not res:
            log.error('Locale browser not in chrome.manifest: %s' % self.pk)
            return ''

        try:
            p = res.groups()[1]
            if 'localepicker.properties' not in p:
                p = os.path.join(p, 'localepicker.properties')
            res = zip.extract_from_manifest(p)
        except (zipfile.BadZipfile, IOError), e:
            log.error('Error unzipping: %s, %s in file: %s' % (p, e, self.pk))
            return ''
        except (ValueError, KeyError), e:
            log.error('No file named: %s in file: %s' % (e, self.pk))
            return ''

        end = time.time() - start
        log.info('Extracted localepicker file: %s in %.2fs' %
                 (self.pk, end))
        statsd.timing('files.extract.localepicker', (end * 1000))
        return res

    def watermark_install_rdf(self, user):
        """
        Reads the install_rdf out of an addon and writes the user information
        into it.
        """
        inzip = SafeUnzip(self.file_path)
        inzip.is_valid()

        try:
            install = inzip.extract_path('install.rdf')
            data = RDF(install)
            data.set(user.email)
        except Exception, e:
            log.error('Could not alter install.rdf in file: %s for %s, %s'
                      % (self.pk, user.pk, e))
            raise

        return data

    def write_watermarked_addon(self, dest, data):
        """
        Writes the watermarked addon to the destination given
        the addons install.rdf data.
        """
        directory = os.path.dirname(dest)
        if not os.path.exists(directory):
            os.makedirs(directory)

        shutil.copyfile(self.file_path, dest)
        outzip = SafeUnzip(dest, mode='w')
        outzip.is_valid()
        outzip.zip.writestr('install.rdf', str(data))

    def watermark(self, user):
        """
        Creates a copy of the file and watermarks the
        metadata with the users.email. If something goes wrong, will
        raise an error, will return the dest if its ready to be served.
        """
        dest = self.watermarked_file_path(user.pk)

        with amo.utils.guard('marketplace.watermark.%s' % dest) as locked:
            if locked:
                # The calling method will need to do something about this.
                log.error('Watermarking in progress of: %s for %s' %
                          (self.pk, user.pk))
                return

            #TODO(andym): re-use or automatically mark stale old watermarks.
            with statsd.timer('marketplace.watermark'):
                log.info('Starting watermarking of: %s for %s' %
                         (self.pk, user.pk))
                data = self.watermark_install_rdf(user)
                self.write_watermarked_addon(dest, data)

        return dest


@receiver(models.signals.post_save, sender=File,
          dispatch_uid='cache_localpicker')
def cache_localepicker(sender, instance, **kw):
    if kw.get('raw') or not kw.get('created'):
        return

    try:
        addon = instance.version.addon
    except models.ObjectDoesNotExist:
        return

    if addon.type == amo.ADDON_LPAPP and addon.status == amo.STATUS_PUBLIC:
        log.info('Updating localepicker for file: %s, addon: %s' %
                 (instance.pk, addon.pk))
        instance.get_localepicker()


@receiver(models.signals.post_delete, sender=File,
          dispatch_uid='version_update_status')
def update_status(sender, instance, **kw):
    if not kw.get('raw'):
        try:
            instance.version.addon.update_status(using='default')
        except models.ObjectDoesNotExist:
            pass


@receiver(models.signals.post_delete, sender=File,
          dispatch_uid='cleanup_file')
def cleanup_file(sender, instance, **kw):
    """ On delete of the file object from the database, unlink the file from
    the file system """
    if kw.get('raw') or not instance.filename:
        return
    # Use getattr so the paths are accessed inside the try block.
    for path in ('file_path', 'mirror_file_path', 'guarded_file_path'):
        try:
            filename = getattr(instance, path)
        except models.ObjectDoesNotExist:
            return
        if os.path.exists(filename):
            os.remove(filename)


@File.on_change
def check_file(old_attr, new_attr, instance, sender, **kw):
    if kw.get('raw'):
        return
    old, new = old_attr.get('status'), instance.status
    if new == amo.STATUS_DISABLED and old != amo.STATUS_DISABLED:
        instance.hide_disabled_file()
    elif old == amo.STATUS_DISABLED and new != amo.STATUS_DISABLED:
        instance.unhide_disabled_file()

    # Log that the hash has changed.
    old, new = old_attr.get('hash'), instance.hash
    if old != new:
        try:
            addon = instance.version.addon.pk
        except models.ObjectDoesNotExist:
            addon = 'unknown'
        log.info('Hash changed for file: %s, addon: %s, from: %s to: %s' %
                 (instance.pk, addon, old, new))


# TODO(davedash): Get rid of this table once /editors is on zamboni
class Approval(amo.models.ModelBase):

    reviewtype = models.CharField(max_length=10, default='pending')
    action = models.IntegerField(default=0)
    os = models.CharField(max_length=255, default='')
    applications = models.CharField(max_length=255, default='')
    comments = models.TextField(null=True)

    addon = models.ForeignKey('addons.Addon')
    user = models.ForeignKey('users.UserProfile')
    file = models.ForeignKey(File)
    reply_to = models.ForeignKey('self', null=True, db_column='reply_to')

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'approvals'
        ordering = ('-created',)


class Platform(amo.models.ModelBase):
    # `name` and `shortname` are provided in amo.__init__
    # name = TranslatedField()
    # shortname = TranslatedField()
    # icondata => mysql blob
    icontype = models.CharField(max_length=25, default='')

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'platforms'

    def __unicode__(self):
        if self.id in amo.PLATFORMS:
            return unicode(amo.PLATFORMS[self.id].name)
        else:
            log.warning('Invalid platform')
            return ''


class FileUpload(amo.models.ModelBase):
    """Created when a file is uploaded for validation/submission."""
    uuid = UUIDField(primary_key=True, auto=True)
    path = models.CharField(max_length=255, default='')
    name = models.CharField(max_length=255, default='',
                            help_text="The user's original filename")
    hash = models.CharField(max_length=255, default='')
    user = models.ForeignKey('users.UserProfile', null=True)
    valid = models.BooleanField(default=False)
    validation = models.TextField(null=True)
    compat_with_app = models.ForeignKey(Application, null=True,
                                    related_name='uploads_compat_for_app')
    compat_with_appver = models.ForeignKey(AppVersion, null=True,
                                    related_name='uploads_compat_for_appver')
    task_error = models.TextField(null=True)

    objects = amo.models.UncachedManagerBase()

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'file_uploads'

    def __unicode__(self):
        return self.uuid

    def save(self, *args, **kw):
        if self.validation:
            try:
                if json.loads(self.validation)['errors'] == 0:
                    self.valid = True
            except Exception:
                log.error('Invalid validation json: %r' % self)
        super(FileUpload, self).save()

    def add_file(self, chunks, filename, size):
        filename = smart_str(filename)
        loc = path.path(settings.ADDONS_PATH) / 'temp' / uuid.uuid4().hex
        if not loc.dirname().exists():
            loc.dirname().makedirs()
        ext = path.path(filename).ext
        if ext in EXTENSIONS:
            loc += ext
        log.info('UPLOAD: %r (%s bytes) to %r' % (filename, size, loc))
        hash = hashlib.sha256()
        with open(loc, 'wb') as fd:
            for chunk in chunks:
                hash.update(chunk)
                fd.write(chunk)
        self.path = loc
        self.name = filename
        self.hash = 'sha256:%s' % hash.hexdigest()
        self.save()

    @classmethod
    def from_post(cls, chunks, filename, size):
        fu = FileUpload()
        fu.add_file(chunks, filename, size)
        return fu


class FileValidation(amo.models.ModelBase):
    file = models.OneToOneField(File, related_name='validation')
    valid = models.BooleanField(default=False)
    errors = models.IntegerField(default=0)
    warnings = models.IntegerField(default=0)
    notices = models.IntegerField(default=0)
    validation = models.TextField()

    class Meta:
        db_table = 'file_validation'

    @classmethod
    def from_json(cls, file, validation):
        js = json.loads(validation)
        new = cls(file=file, validation=validation, errors=js['errors'],
                  warnings=js['warnings'], notices=js['notices'])
        new.valid = new.errors == 0
        if ('metadata' in js and
            js['metadata'].get('contains_binary_extension', False)):
            file.version.addon.update(binary=True)
        new.save()
        return new


class TestCase(amo.models.ModelBase):
    test_group = models.ForeignKey('TestGroup')
    help_link = models.CharField(max_length=255, blank=True,
            help_text='Deprecated')
    function = models.CharField(max_length=255,
            help_text='Name of the function to call')

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'test_cases'


class TestGroup(amo.models.ModelBase):
    category = models.CharField(max_length=255, blank=True)
    tier = models.PositiveSmallIntegerField(default=2,
            help_text="Run in order.  Tier 1 runs before Tier 2, etc.")
    critical = models.BooleanField(default=False,
            help_text="Should this group failing stop all tests?")
    types = models.PositiveIntegerField(default=0,
            help_text="Pretty sure it involves binary math... KHAN!!!")

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'test_groups'


class TestResult(amo.models.ModelBase):
    file = models.ForeignKey(File)
    test_case = models.ForeignKey(TestCase)
    result = models.PositiveSmallIntegerField(default=0)
    line = models.PositiveIntegerField(default=0)
    filename = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'test_results'


class TestResultCache(models.Model):
    """When a file is checked the results are stored here in JSON.  This is
    temporary storage and removed with the garbage cleanup cron."""
    date = models.DateTimeField()
    key = models.CharField(max_length=255, db_index=True)
    test_case = models.ForeignKey(TestCase)
    value = models.TextField(blank=True)

    class Meta(amo.models.ModelBase.Meta):
        db_table = 'test_results_cache'


def nfd_str(u):
    """Uses NFD to normalize unicode strings."""
    if isinstance(u, unicode):
        return unicodedata.normalize('NFD', u).encode('utf-8')
    return u


@django.dispatch.receiver(devhub.signals.submission_done)
def check_jetpack_version(sender, **kw):
    import files.tasks
    from files.utils import JetpackUpgrader

    minver, maxver = JetpackUpgrader().jetpack_versions()
    qs = File.objects.filter(version__addon=sender,
                             jetpack_version__isnull=False)
    ids = [f.id for f in qs
           if vint(minver) <= vint(f.jetpack_version) < vint(maxver)]
    if ids:
        files.tasks.start_upgrade.delay(ids, priority='high')
