"""Minimal Django settings for running the SCORM XBlock tests outside edx-platform."""
import tempfile

SECRET_KEY = 'scorm-tests'  # test-only value
DEBUG = False
USE_TZ = True
INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
MEDIA_ROOT = tempfile.mkdtemp(prefix='scorm-media-')
MEDIA_URL = '/media/'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': False, 'OPTIONS': {}}]
HTTPS = 'on'
LMS_BASE = 'lms.example.org'
XBLOCK_SETTINGS = {}
