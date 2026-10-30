"""
SCORM XBlock (ilmX fork of raccoongang/edx_xblock_scorm, ported to Open edX Verawood).

Block type ``scormxblock`` and all field names/scopes are unchanged from the Juniper
fork so that existing course content and learner state keep working.

ilmX behaviour (see PORT-NOTES-upstream-blocks.md in the ilmX-verawood repo):

* packages are stored in Django ``default_storage`` (S3 in production) under
  ``scorm/<org>/<course>/<block_id>/<sha1>.zip`` and extracted next to the zip under
  ``scorm/<org>/<course>/<block_id>/<sha1>/``; nothing is kept on the local disk;
* re-uploading the same package to the same block is a no-op;
* the grade is published only once the SCORM lesson is completed/passed/failed;
* the score line above the player is hidden and the iframe is rendered above the
  full-screen buttons;
* package files are served from the LMS origin so the SCORM runtime in the iframe can
  reach ``window.parent.API``.  Juniper used an nginx ``/s3-proxy`` location for this; the
  Verawood port serves them through the ``scorm_proxy`` XBlock handler by default (see
  ``SERVE_MODE`` below).

Settings (``XBLOCK_SETTINGS['scormxblock']``):

* ``SERVE_MODE``: ``"proxy"`` (default, XBlock handler), ``"s3_proxy"`` (legacy Juniper nginx
  ``/s3-proxy`` location) or ``"storage_url"`` (link directly to ``default_storage.url()``;
  only works when that URL is on the LMS origin).
* ``S3_PROXY_PREFIX``: URL prefix for ``s3_proxy`` mode (default ``"/s3-proxy"``).
"""
import hashlib
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import tempfile
import urllib.parse
import zipfile
from functools import partial
from importlib import resources

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.template import Context, Template
from django.utils import timezone
from web_fragments.fragment import Fragment
from webob import Response
from webob.static import FileIter
from xblock.core import XBlock
from xblock.fields import Boolean, DateTime, Dict, Float, Integer, Scope, String

try:  # XBlock >= 2 ships the former xblock-utils
    from xblock.utils.resources import ResourceLoader
    from xblock.utils.settings import XBlockWithSettingsMixin
except ImportError:  # pragma: no cover
    from xblockutils.resources import ResourceLoader
    from xblockutils.settings import XBlockWithSettingsMixin

try:  # manifests are uploaded by course staff, but parse them defensively anyway
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET  # nosec

try:
    from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
except ImportError:  # outside edx-platform (tests, workbench)
    configuration_helpers = None


# Make '_' a no-op so we can scrape strings
def _(text):
    return text


loader = ResourceLoader(__name__)
log = logging.getLogger(__name__)

SERVE_MODE_PROXY = 'proxy'
SERVE_MODE_S3_PROXY = 's3_proxy'
SERVE_MODE_STORAGE_URL = 'storage_url'

# SCORM packages often ship JS/fonts with extensions the stdlib does not map everywhere.
for _mime, _ext in (('application/javascript', '.js'), ('font/woff', '.woff'), ('font/woff2', '.woff2'),
                    ('image/svg+xml', '.svg'), ('application/json', '.json')):
    mimetypes.add_type(_mime, _ext)


class ScormPackageError(Exception):
    """Raised for invalid SCORM packages or asset paths."""


@XBlock.needs('i18n')
@XBlock.wants('user')
@XBlock.wants('settings')
class ScormXBlock(XBlockWithSettingsMixin, XBlock):
    """
    XBlock that plays an uploaded SCORM 1.2 / 2004 package in an iframe.
    """

    # Do not clash with overhangio's openedx-scorm-xblock, which also uses a class named
    # ScormXBlock (and therefore XBLOCK_SETTINGS['ScormXBlock']).
    block_settings_key = 'scormxblock'

    display_name = String(
        display_name=_("Display Name"),
        help=_("Display name for this module"),
        default="Scorm",
        scope=Scope.settings,
    )
    scorm_file = String(
        display_name=_("Upload scorm file"),
        scope=Scope.settings,
    )
    path_index_page = String(
        display_name=_("Path to the index page in scorm file"),
        scope=Scope.settings,
    )
    scorm_file_meta = Dict(
        scope=Scope.content
    )
    version_scorm = String(
        default="SCORM_12",
        scope=Scope.settings,
    )
    # save completion_status for SCORM_2004
    lesson_status = String(
        scope=Scope.user_state,
        default='not attempted'
    )
    success_status = String(
        scope=Scope.user_state,
        default='unknown'
    )
    data_scorm = Dict(
        scope=Scope.user_state,
        default={}
    )
    lesson_score = Float(
        scope=Scope.user_state,
        default=0
    )
    weight = Float(
        default=1,
        scope=Scope.settings
    )
    has_score = Boolean(
        display_name=_("Scored"),
        help=_("Select False if this component will not receive a numerical score from the Scorm"),
        default=True,
        scope=Scope.settings
    )
    icon_class = String(
        default="video",
        scope=Scope.settings,
    )
    width = Integer(
        display_name=_("Display Width (px)"),
        help=_('Width of iframe, if empty, the default 100'),
        scope=Scope.settings
    )
    height = Integer(
        display_name=_("Display Height (px)"),
        help=_('Height of iframe'),
        default=450,
        scope=Scope.settings
    )

    has_author_view = True

    @staticmethod
    def resource_string(path):
        """Handy helper for getting resources from our kit."""
        return resources.files(__package__).joinpath(path).read_text(encoding='utf-8')

    # ------------------------------------------------------------------ helpers

    @property
    def _location(self):
        """Usage key of this block (``location`` is only mixed in inside edx-platform)."""
        return getattr(self, 'location', None) or self.scope_ids.usage_id

    @property
    def _is_scored(self):
        """
        ``has_score`` as a real boolean.

        Studio used to store the raw form value ("True"/"False" strings) in this field.
        """
        value = self.has_score
        if isinstance(value, str):
            return value.strip().lower() == 'true'
        return bool(value)

    def _xblock_setting(self, name, default=None):
        xblock_settings = self.get_xblock_settings(default={}) or {}
        return xblock_settings.get(name, default)

    # -------------------------------------------------------------------- views

    def student_view(self, context=None):
        context_html = self.get_context_student()
        template = loader.render_django_template(
            'static/html/scormxblock.html',
            context=context_html,
            i18n_service=self.runtime.service(self, 'i18n')
        )
        frag = Fragment(template)
        frag.add_css(self.resource_string("static/css/scormxblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/scormxblock.js"))
        js_settings = {
            'version_scorm': self.version_scorm
        }
        frag.initialize_js('ScormXBlock', json_args=js_settings)
        return frag

    def studio_view(self, context=None):
        context_html = self.get_context_studio()
        template = loader.render_django_template(
            'static/html/studio.html',
            context=context_html,
            i18n_service=self.runtime.service(self, 'i18n')
        )
        frag = Fragment(template)
        frag.add_css(self.resource_string("static/css/scormxblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/studio.js"))
        frag.initialize_js('ScormStudioXBlock')
        return frag

    def author_view(self, context=None):
        html = loader.render_django_template(
            "static/html/author_view.html",
            context=context,
            i18n_service=self.runtime.service(self, 'i18n')
        )
        frag = Fragment(html)
        return frag

    # ------------------------------------------------------------ studio upload

    @XBlock.handler
    def studio_submit(self, request, suffix=''):
        self.display_name = request.params['display_name']
        self.width = request.params['width']
        self.height = request.params['height']
        self.has_score = request.params['has_score']
        self.icon_class = 'problem' if self._is_scored else 'video'

        if hasattr(request.params['file'], 'file'):
            scorm_file = request.params['file'].file

            # First, save scorm file in the storage for mobile clients
            self.scorm_file_meta['sha1'] = self.get_sha1(scorm_file)
            self.scorm_file_meta['name'] = scorm_file.name
            self.scorm_file_meta['path'] = path = self._file_storage_path()
            self.scorm_file_meta['last_updated'] = timezone.now().strftime(DateTime.DATETIME_FORMAT)

            if default_storage.exists(path):
                # ilmX: the same package was already uploaded (and extracted) for this block.
                return Response(
                    json.dumps({'result': 'same file uploaded previously'}),
                    content_type='application/json', charset="utf8",
                )

            package_root = self._package_root_from_zip_path(path)
            extract_dir = tempfile.mkdtemp(prefix='scorm-')
            try:
                # ilmX: extract before saving anything to default storage, so a broken zip
                # does not leave a half-uploaded package behind.
                self.extract_package(scorm_file, extract_dir)

                scorm_file.seek(0)
                default_storage.save(path, File(scorm_file))
                self.scorm_file_meta['size'] = default_storage.size(path)
                log.info('"%s" file stored at "%s"', scorm_file, path)

                for root, _dirs, files in os.walk(extract_dir):
                    for file_name in files:
                        local_file_path = os.path.join(root, file_name)
                        relative_path = os.path.relpath(local_file_path, extract_dir).replace(os.sep, '/')
                        with open(local_file_path, 'rb') as source_file:
                            default_storage.save('{}/{}'.format(package_root, relative_path), File(source_file))

                self.set_fields_xblock(package_root, local_dir=extract_dir)
            except ScormPackageError as exc:
                log.warning('Invalid SCORM package uploaded to %s: %s', self._location, exc)
                return Response(
                    json.dumps({'result': 'error', 'message': str(exc)}),
                    status=400, content_type='application/json', charset="utf8",
                )
            finally:
                shutil.rmtree(extract_dir, ignore_errors=True)

        return Response(json.dumps({'result': 'success'}), content_type='application/json', charset="utf8")

    @staticmethod
    def extract_package(scorm_file, target_dir):
        """
        Safely unzip ``scorm_file`` into ``target_dir``.

        Replaces the Juniper ``os.system('unzip ...')`` call (shell injection via file name,
        no zip-slip protection).
        """
        scorm_file.seek(0)
        try:
            with zipfile.ZipFile(scorm_file) as archive:
                target_root = os.path.realpath(target_dir)
                for member in archive.infolist():
                    destination = os.path.realpath(os.path.join(target_root, member.filename))
                    if destination != target_root and not destination.startswith(target_root + os.sep):
                        raise ScormPackageError('Unsafe path in package: {}'.format(member.filename))
                archive.extractall(target_root)
        except zipfile.BadZipFile as exc:
            raise ScormPackageError('Not a valid zip file') from exc
        finally:
            scorm_file.seek(0)

    # ------------------------------------------------------------- SCORM API

    @XBlock.json_handler
    def scorm_get_value(self, data, suffix=''):
        name = data.get('name')
        user_service = self.runtime.service(self, 'user')
        xb_user = user_service.get_current_user() if user_service else None
        opt_attrs = getattr(xb_user, 'opt_attrs', None) or {}

        if name in ['cmi.core.lesson_status', 'cmi.completion_status']:
            return {'value': self.lesson_status}
        elif name == 'cmi.core.student_id':
            return {'value': opt_attrs.get('edx-platform.user_id')}
        elif name == 'cmi.core.student_name':
            return {'value': opt_attrs.get('edx-platform.username')}
        elif name == 'cmi.success_status':
            return {'value': self.success_status}
        elif name in ['cmi.core.score.raw', 'cmi.score.raw']:
            return {'value': self.lesson_score * 100}
        else:
            return {'value': self.data_scorm.get(name, '')}

    @XBlock.json_handler
    def scorm_set_value(self, data, suffix=''):
        context = {'result': 'success'}
        name = data.get('name')

        if name in ['cmi.core.lesson_status', 'cmi.completion_status']:
            self.lesson_status = data.get('value')
            if self._is_scored and data.get('value') in ['completed', 'failed', 'passed']:
                self.publish_grade()
                context.update({"lesson_score": self.format_lesson_score})

        elif name == 'cmi.success_status':
            self.success_status = data.get('value')
            if self._is_scored:
                if self.success_status == 'unknown':
                    self.lesson_score = 0
                self.publish_grade()
                context.update({"lesson_score": self.format_lesson_score})
        elif name in ['cmi.core.score.raw', 'cmi.score.raw'] and self._is_scored:
            self.lesson_score = float(data.get('value', 0)) / 100.0
            # ilmX: only publish the score once the lesson is finished.
            if self.lesson_status and self.lesson_status in ['completed', 'failed', 'passed']:
                self.publish_grade()
            context.update({"lesson_score": self.format_lesson_score})
        else:
            self.data_scorm[name] = data.get('value', '')

        context.update({"completion_status": self.get_completion_status()})
        return context

    def publish_grade(self):
        if self.lesson_status == 'failed' or (self.version_scorm == 'SCORM_2004'
                                              and self.success_status in ['failed', 'unknown']):
            self.runtime.publish(
                self,
                'grade',
                {
                    'value': 0,
                    'max_value': self.weight,
                })
        else:
            self.runtime.publish(
                self,
                'grade',
                {
                    'value': self.lesson_score,
                    'max_value': self.weight,
                })

    def max_score(self):
        """
        Return the maximum score possible.
        """
        return self.weight if self._is_scored else None

    # ------------------------------------------------------- package serving

    @XBlock.handler
    def scorm_proxy(self, request, suffix=''):
        """
        Serve a file of the extracted package from ``default_storage`` on the LMS origin.

        ``suffix`` is the path of the file relative to the package root, e.g.
        ``.../handler/scorm_proxy/res/index.html``. Range requests are supported so
        audio/video inside packages can be seeked.
        """
        package_root = self.package_root
        if not package_root:
            return Response('No SCORM package', status=404, content_type='text/plain')
        try:
            relative_path = self.clean_asset_path(suffix)
        except ScormPackageError:
            log.warning('Invalid SCORM asset path %r requested for %s', suffix, self._location)
            return Response('Invalid asset path', status=400, content_type='text/plain')

        storage_path = '{}/{}'.format(package_root, relative_path)
        if not default_storage.exists(storage_path):
            return Response('Not found', status=404, content_type='text/plain')

        content_type = mimetypes.guess_type(relative_path)[0] or 'application/octet-stream'
        size = default_storage.size(storage_path)
        storage_file = default_storage.open(storage_path, 'rb')
        # The LMS converts handler responses to Django responses, so WebOb's
        # conditional_response machinery never runs: handle Range requests here.
        content_range = request.range.content_range(size) if getattr(request, 'range', None) else None
        if content_range:
            with storage_file:
                storage_file.seek(content_range.start)
                body = storage_file.read(content_range.stop - content_range.start)
            response = Response(body=body, status=206, content_type=content_type)
            response.headers['Content-Range'] = str(content_range)
        else:
            response = Response(app_iter=FileIter(storage_file), content_type=content_type)
            response.content_length = size
        response.headers['Accept-Ranges'] = 'bytes'
        response.cache_control.private = True
        response.cache_control.max_age = 3600
        return response

    @staticmethod
    def clean_asset_path(path):
        """
        Validate a package-relative asset path (no traversal, no absolute paths).
        """
        cleaned = urllib.parse.unquote((path or '').split('?')[0]).replace('\\', '/')
        if not cleaned or '\x00' in cleaned or cleaned.endswith('/'):
            raise ScormPackageError('Invalid asset path: {!r}'.format(path))
        if cleaned.startswith('/') or re.match(r'^[A-Za-z]:', cleaned) or '..' in cleaned.split('/'):
            raise ScormPackageError('Invalid asset path: {!r}'.format(path))
        normalized = posixpath.normpath(cleaned)
        if normalized in ('.', '') or normalized.startswith('../'):
            raise ScormPackageError('Invalid asset path: {!r}'.format(path))
        return normalized

    @property
    def storage_file_path(self):
        """
        ``scorm_file`` as a ``default_storage`` path.

        ilmX values look like ``scorm/<org>/<course>/<block_id>/<sha1>/<index page>``.
        Blocks uploaded before Dec 2024 stored a MEDIA_URL based URL path
        (``/media/scorm/<block_id>/<index page>``); the MEDIA_URL prefix is stripped.
        """
        scorm_file = self.scorm_file or ''
        media_url = getattr(settings, 'MEDIA_URL', '') or ''
        if media_url and media_url != '/' and scorm_file.startswith(media_url):
            scorm_file = scorm_file[len(media_url):]
        return scorm_file.lstrip('/')

    @property
    def package_root(self):
        """Storage directory the package was extracted to (without trailing slash)."""
        file_path = self.storage_file_path
        index_page = (self.path_index_page or '').lstrip('/')
        if not file_path:
            return ''
        if index_page and file_path.endswith('/' + index_page):
            return file_path[:-(len(index_page) + 1)]
        return posixpath.dirname(file_path)

    def _site_root(self):
        scheme = 'https' if getattr(settings, 'HTTPS', 'on') == 'on' else 'http'
        lms_base = getattr(settings, 'LMS_BASE', '')
        domain = configuration_helpers.get_value('site_domain', lms_base) if configuration_helpers else lms_base
        return '{}://{}'.format(scheme, domain)

    def get_scorm_file_url(self):
        """URL of the package's index page, as loaded by the learner iframe."""
        if not self.scorm_file:
            return ''
        mode = self._xblock_setting('SERVE_MODE', SERVE_MODE_PROXY)
        if mode == SERVE_MODE_S3_PROXY:
            # Legacy Juniper behaviour: nginx location proxying the S3 bucket on the LMS domain.
            prefix = self._xblock_setting('S3_PROXY_PREFIX', '/s3-proxy')
            s3_url_path = urllib.parse.urlparse(default_storage.url(self.storage_file_path)).path
            return '{}{}{}'.format(self._site_root(), prefix, s3_url_path)
        if mode == SERVE_MODE_STORAGE_URL:
            return default_storage.url(self.storage_file_path)
        index_page = self.storage_file_path[len(self.package_root):].lstrip('/')
        handler_url = self.runtime.handler_url(self, 'scorm_proxy').rstrip('?/')
        return '{}/{}'.format(handler_url, urllib.parse.quote(index_page))

    def get_context_studio(self):
        return {
            'field_display_name': self.fields['display_name'],
            'field_scorm_file': self.fields['scorm_file'],
            'field_has_score': self.fields['has_score'],
            'field_width': self.fields['width'],
            'field_height': self.fields['height'],
            'scorm_xblock': self
        }

    def get_context_student(self):
        return {
            'scorm_file_path': self.get_scorm_file_url(),
            'completion_status': self.get_completion_status(),
            'scorm_xblock': self
        }

    def render_template(self, template_path, context):
        template_str = self.resource_string(template_path)
        template = Template(template_str)
        return template.render(Context(context))

    def set_fields_xblock(self, package_root, local_dir=None):
        """
        Read ``imsmanifest.xml`` and set the index page / SCORM version fields.

        ``package_root`` is the storage directory of the extracted package; the manifest
        is read from ``local_dir`` when given (freshly extracted upload), otherwise from
        ``default_storage``.
        """
        self.path_index_page = 'index.html'
        manifest = self._open_manifest(package_root, local_dir)
        if manifest is not None:
            with manifest:
                manifest_bytes = manifest.read()
            root = ET.fromstring(manifest_bytes)
            match = re.match(r'^\{(.*)\}', root.tag)
            namespace = match.group(1) if match else ''

            if namespace:
                resource = root.find('{{{0}}}resources/{{{0}}}resource'.format(namespace))
                schemaversion = root.find('{{{0}}}metadata/{{{0}}}schemaversion'.format(namespace))
            else:
                resource = root.find('resources/resource')
                schemaversion = root.find('metadata/schemaversion')

            if resource is not None and resource.get('href'):
                self.path_index_page = resource.get('href')
            if (schemaversion is not None) and (re.match('^1.2$', schemaversion.text or '') is None):
                self.version_scorm = 'SCORM_2004'
            else:
                self.version_scorm = 'SCORM_12'

        self.scorm_file = '{}/{}'.format(package_root, self.path_index_page)

    @staticmethod
    def _open_manifest(package_root, local_dir):
        if local_dir:
            local_path = os.path.join(local_dir, 'imsmanifest.xml')
            return open(local_path, 'rb') if os.path.exists(local_path) else None  # noqa: SIM115
        storage_path = '{}/imsmanifest.xml'.format(package_root)
        if default_storage.exists(storage_path):
            return default_storage.open(storage_path, 'rb')
        return None

    def get_completion_status(self):
        completion_status = self.lesson_status
        if self.version_scorm == 'SCORM_2004' and self.success_status != 'unknown':
            completion_status = self.success_status
        return completion_status

    def _file_storage_path(self):
        """
        Get file path of storage (ilmX layout: ``scorm/<org>/<course>/<block_id>/<sha1><ext>``).
        """
        loc = self._location
        course_key = getattr(loc, 'course_key', None)
        org = getattr(loc, 'org', None) or getattr(course_key, 'org', '')
        course = getattr(loc, 'course', None) or getattr(course_key, 'course', '')
        path = (
            'scorm/{org}/{course}/{block_id}'
            '/{sha1}{ext}'.format(
                org=org,
                course=course,
                block_id=loc.block_id,
                sha1=self.scorm_file_meta['sha1'],
                ext=os.path.splitext(self.scorm_file_meta['name'])[1]
            )
        )
        return path

    @staticmethod
    def _package_root_from_zip_path(path):
        return os.path.splitext(path)[0]

    @staticmethod
    def get_sha1(file_descriptor):
        """
        Get file hex digest (fingerprint).
        """
        block_size = 8 * 1024
        sha1 = hashlib.sha1()
        for block in iter(partial(file_descriptor.read, block_size), b''):
            sha1.update(block)
        file_descriptor.seek(0)
        return sha1.hexdigest()

    @property
    def format_lesson_score(self):
        return '{:.2f}'.format(self.lesson_score)

    def student_view_data(self, context=None):
        """
        Inform REST api clients about original file location and it's "freshness".
        Make sure to include `student_view_data=scormxblock` to URL params in the request.
        """
        if self.scorm_file and self.scorm_file_meta:
            return {
                'last_modified': self.scorm_file_meta.get('last_updated', ''),
                'scorm_data': default_storage.url(self._file_storage_path()),
                'size': self.scorm_file_meta.get('size', 0),
                'index_page': self.path_index_page,
            }
        return {}

    @staticmethod
    def workbench_scenarios():
        """A canned scenario for display in the workbench."""
        return [
            ("ScormXBlock",
             """<vertical_demo>
                <scormxblock/>
                </vertical_demo>
             """),
        ]
