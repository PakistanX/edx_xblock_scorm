"""
Tests for the ilmX behaviour of the SCORM XBlock, using real zip files and a
FileSystemStorage-backed ``default_storage`` (see test_settings.py).
"""
import hashlib
import io
import json
import shutil
import zipfile
from importlib.metadata import entry_points
from unittest import mock

from django.conf import settings
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, override_settings
from webob import Request
from xblock.field_data import DictFieldData
from xblock.fields import Scope

from scormxblock import ScormXBlock

MANIFEST_12 = b"""<?xml version="1.0"?>
<manifest identifier="m" version="1" xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2">
  <metadata><schema>ADL SCORM</schema><schemaversion>1.2</schemaversion></metadata>
  <organizations/>
  <resources>
    <resource identifier="r1" type="webcontent" href="res/index.html"><file href="res/index.html"/></resource>
  </resources>
</manifest>
"""

MANIFEST_2004 = MANIFEST_12.replace(b'<schemaversion>1.2</schemaversion>',
                                    b'<schemaversion>2004 4th Edition</schemaversion>')


def make_zip(files):
    """Return an in-memory zip (BytesIO with a ``name``) containing ``files``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    buf.seek(0)
    buf.name = 'package.zip'
    return buf


def package_files(manifest=MANIFEST_12):
    return {
        'imsmanifest.xml': manifest,
        'res/index.html': b'<html><body>SCORM</body></html>',
        'res/app.js': b'console.log("hi");',
        'res/media/clip.mp3': bytes(range(256)) * 4,
    }


class IlmxScormTestCase(SimpleTestCase):
    """Base class: a block with a fake runtime and a clean storage."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        self.published = []
        self.runtime = mock.Mock()
        self.runtime.service.return_value = None
        self.runtime.handler_url.side_effect = lambda block, handler, *a, **kw: (
            '/courses/course-v1:org+course+run/xblock/block-v1:org+course+run+type@scormxblock+block@'
            'block_id/handler/{}'.format(handler)
        )
        self.runtime.publish.side_effect = lambda block, event, data: self.published.append((event, data))

    def make_block(self, **fields):
        block = ScormXBlock(self.runtime, DictFieldData(fields), mock.Mock())
        block.location = mock.Mock(block_id='block_id', org='org', course='course', block_type='scormxblock')
        return block

    def upload(self, block, zip_file, has_score='True'):
        params = {
            'display_name': 'SCORM', 'width': '', 'height': '450', 'has_score': has_score,
            'file': mock.Mock(file=zip_file),
        }
        return block.studio_submit(mock.Mock(method='POST', params=params))


class TestIlmxUpload(IlmxScormTestCase):
    """Upload / extraction into default_storage."""

    def test_upload_stores_zip_and_extracted_files(self):
        block = self.make_block()
        zip_file = make_zip(package_files())
        sha1 = hashlib.sha1(zip_file.getvalue()).hexdigest()

        response = self.upload(block, zip_file)

        self.assertEqual(json.loads(response.body), {'result': 'success'})
        root = 'scorm/org/course/block_id/{}'.format(sha1)
        self.assertTrue(default_storage.exists(root + '.zip'))
        for name in ('imsmanifest.xml', 'res/index.html', 'res/app.js', 'res/media/clip.mp3'):
            self.assertTrue(default_storage.exists('{}/{}'.format(root, name)), name)
        self.assertEqual(block.scorm_file, root + '/res/index.html')
        self.assertEqual(block.path_index_page, 'res/index.html')
        self.assertEqual(block.version_scorm, 'SCORM_12')
        self.assertEqual(block.scorm_file_meta['path'], root + '.zip')
        self.assertEqual(block.scorm_file_meta['sha1'], sha1)
        self.assertEqual(block.scorm_file_meta['size'], len(zip_file.getvalue()))
        self.assertEqual(block.package_root, root)
        self.assertEqual(block.icon_class, 'problem')

    def test_scorm_2004_detected(self):
        block = self.make_block()
        self.upload(block, make_zip(package_files(MANIFEST_2004)))
        self.assertEqual(block.version_scorm, 'SCORM_2004')

    def test_no_manifest_defaults_to_index_html(self):
        block = self.make_block()
        self.upload(block, make_zip({'index.html': b'x'}))
        self.assertEqual(block.path_index_page, 'index.html')
        self.assertTrue(block.scorm_file.endswith('/index.html'))

    def test_same_package_uploaded_twice_is_noop(self):
        block = self.make_block()
        files = package_files()
        self.upload(block, make_zip(files))
        first_scorm_file = block.scorm_file

        response = self.upload(block, make_zip(files))

        self.assertEqual(json.loads(response.body), {'result': 'same file uploaded previously'})
        self.assertEqual(block.scorm_file, first_scorm_file)

    def test_zip_slip_rejected_and_nothing_stored(self):
        block = self.make_block()
        response = self.upload(block, make_zip({'../../evil.html': b'x', 'imsmanifest.xml': MANIFEST_12}))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(default_storage.exists('scorm'))
        self.assertIsNone(block.scorm_file)

    def test_invalid_zip_rejected(self):
        block = self.make_block()
        bad = io.BytesIO(b'not a zip')
        bad.name = 'bad.zip'
        response = self.upload(block, bad)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(default_storage.exists('scorm'))

    def test_settings_saved_without_file(self):
        block = self.make_block()
        params = {'display_name': 'New', 'width': '800', 'height': '600', 'has_score': 'False', 'file': ''}
        block.studio_submit(mock.Mock(method='POST', params=params))
        self.assertEqual(block.display_name, 'New')
        self.assertEqual(block.icon_class, 'video')
        self.assertIsNone(block.max_score())


class TestIlmxGrading(IlmxScormTestCase):
    """ilmX: the grade is only published once the lesson is finished."""

    def set_value(self, block, name, value):
        body = json.dumps({'name': name, 'value': value}).encode('utf-8')
        return block.scorm_set_value(mock.Mock(method='POST', body=body))

    def test_raw_score_not_published_before_completion(self):
        block = self.make_block(has_score=True)
        self.set_value(block, 'cmi.core.score.raw', '80')
        self.assertEqual(block.lesson_score, 0.8)
        self.assertEqual(self.published, [])

    def test_raw_score_published_after_completion(self):
        block = self.make_block(has_score=True, weight=2)
        self.set_value(block, 'cmi.core.lesson_status', 'completed')
        self.published.clear()
        self.set_value(block, 'cmi.core.score.raw', '80')
        self.assertEqual(self.published, [('grade', {'value': 0.8, 'max_value': 2})])

    def test_failed_publishes_zero(self):
        block = self.make_block(has_score=True, lesson_score=0.9)
        self.set_value(block, 'cmi.core.lesson_status', 'failed')
        self.assertEqual(self.published, [('grade', {'value': 0, 'max_value': 1})])

    def test_legacy_string_has_score_false_is_not_scored(self):
        """Studio stored has_score as the string "False" in Juniper."""
        block = self.make_block(has_score='False')
        self.set_value(block, 'cmi.core.lesson_status', 'completed')
        self.set_value(block, 'cmi.core.score.raw', '80')
        self.assertEqual(self.published, [])
        self.assertIsNone(block.max_score())


class TestIlmxServing(IlmxScormTestCase):
    """Package files are served from the LMS origin."""

    def setUp(self):
        super().setUp()
        self.block = self.make_block()
        self.upload(self.block, make_zip(package_files()))

    def proxy(self, suffix, **headers):
        request = Request.blank('/' + suffix, headers=headers)
        return self.block.scorm_proxy(request, suffix)

    def test_default_mode_uses_handler(self):
        url = self.block.get_scorm_file_url()
        self.assertTrue(url.endswith('/handler/scorm_proxy/res/index.html'), url)
        self.assertTrue(url.startswith('/courses/'), url)

    def test_proxy_serves_file(self):
        response = self.proxy('res/app.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'application/javascript')
        self.assertEqual(response.body, b'console.log("hi");')

    def test_proxy_supports_ranges(self):
        response = self.proxy('res/media/clip.mp3', Range='bytes=0-9')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.body, bytes(range(10)))
        self.assertEqual(response.headers['Content-Range'], 'bytes 0-9/1024')

    def test_proxy_rejects_traversal(self):
        for suffix in ('../other/secret.txt', 'res/../../x', '/etc/passwd', '%2e%2e/x', ''):
            self.assertEqual(self.proxy(suffix).status_code, 400, suffix)

    def test_proxy_missing_file(self):
        self.assertEqual(self.proxy('res/missing.js').status_code, 404)

    def test_proxy_without_package(self):
        block = self.make_block()
        response = block.scorm_proxy(Request.blank('/x'), 'index.html')
        self.assertEqual(response.status_code, 404)

    @override_settings(XBLOCK_SETTINGS={'scormxblock': {'SERVE_MODE': 's3_proxy'}})
    def test_legacy_s3_proxy_mode(self):
        with mock.patch.object(ScormXBlock, 'get_xblock_settings',
                               return_value={'SERVE_MODE': 's3_proxy'}), \
                mock.patch('scormxblock.scormxblock.default_storage.url',
                           return_value='https://bucket.s3.amazonaws.com/scorm/org/course/x/res/index.html'):
            url = self.block.get_scorm_file_url()
        self.assertEqual(url, 'https://lms.example.org/s3-proxy/scorm/org/course/x/res/index.html')

    def test_storage_url_mode(self):
        with mock.patch.object(ScormXBlock, 'get_xblock_settings', return_value={'SERVE_MODE': 'storage_url'}):
            url = self.block.get_scorm_file_url()
        self.assertEqual(url, '/media/' + self.block.scorm_file)

    def test_legacy_media_url_value(self):
        """Blocks uploaded before Dec 2024 stored '/media/scorm/<block_id>/<index>'."""
        block = self.make_block(scorm_file='/media/scorm/block_id/index.html', path_index_page='index.html')
        self.assertEqual(block.storage_file_path, 'scorm/block_id/index.html')
        self.assertEqual(block.package_root, 'scorm/block_id')

    def test_student_view_layout(self):
        html = self.block.student_view().content
        self.assertIn('/handler/scorm_proxy/res/index.html', html)
        self.assertLess(html.index('<iframe'), html.index('js-button-full-screen'))
        self.assertNotIn('lesson_score', html)


class TestIlmxCompatibility(SimpleTestCase):
    """Block type, field names and scopes must stay identical to Juniper."""

    def test_entry_point(self):
        eps = {ep.name: ep.value for ep in entry_points(group='xblock.v1')}
        self.assertEqual(eps.get('scormxblock'), 'scormxblock:ScormXBlock')

    def test_fields_and_scopes(self):
        expected = {
            'display_name': Scope.settings, 'scorm_file': Scope.settings, 'path_index_page': Scope.settings,
            'scorm_file_meta': Scope.content, 'version_scorm': Scope.settings,
            'lesson_status': Scope.user_state, 'success_status': Scope.user_state,
            'data_scorm': Scope.user_state, 'lesson_score': Scope.user_state, 'weight': Scope.settings,
            'has_score': Scope.settings, 'icon_class': Scope.settings, 'width': Scope.settings,
            'height': Scope.settings,
        }
        for name, scope in expected.items():
            self.assertIn(name, ScormXBlock.fields)
            self.assertEqual(ScormXBlock.fields[name].scope, scope, name)

    def test_settings_key_does_not_clash_with_overhang_block(self):
        self.assertEqual(ScormXBlock.block_settings_key, 'scormxblock')
