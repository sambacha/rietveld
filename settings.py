# Copyright 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Minimal Django settings."""

import logging
import os

from google.appengine.api import app_identity

# Banner for e.g. planned downtime announcements
## SPECIAL_BANNER = """\
## Rietveld will be down for maintenance on
## Thursday November 17
## from
## <a href="http://www.timeanddate.com/worldclock/fixedtime.html?iso=20111117T17&ah=6">
## 17:00 - 23:00 UTC
## </a>
## """

APPEND_SLASH = False
DEBUG = os.environ["SERVER_SOFTWARE"].startswith("Dev")

# Django requires that settings.SECRET_KEY be defined.  According to the docs,
# It is used by django.core.signing, which we do not use in our app.
# We could just set it to 'foo' and ignore it, but that would not be secure.
# So, we make sure that it is never used for anything.
class MakeSureNothingReadsThisString(object):
    """If Django reads this string for any reason, fail loudly."""

    def __str__(self):
        logging.error("SECRET_KEY was never meant to be used")
        raise NotImplementedError()


SECRET_KEY = MakeSureNothingReadsThisString()


INSTALLED_APPS = ("codereview",)
HSTS_MAX_AGE = 60 * 60 * 24 * 365  # 1 year in seconds.
MIDDLEWARE_CLASSES = (
    "django.middleware.common.CommonMiddleware",
    "django.middleware.http.ConditionalGetMiddleware",
    "codereview.middleware.RedirectToHTTPSMiddleware",
    "codereview.middleware.AddHSTSHeaderMiddleware",
    "codereview.middleware.AddUserToRequestMiddleware",
    "codereview.middleware.PropagateExceptionMiddleware",
)
ROOT_URLCONF = "urls"
TEMPLATE_CONTEXT_PROCESSORS = ("django.core.context_processors.request",)
TEMPLATE_DEBUG = DEBUG
TEMPLATE_DIRS = (os.path.join(os.path.dirname(__file__), "templates"),)
TEMPLATE_LOADERS = (
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
)
FILE_UPLOAD_HANDLERS = ("django.core.files.uploadhandler.MemoryFileUploadHandler",)
FILE_UPLOAD_MAX_MEMORY_SIZE = 1048576  # 1 MB

MEDIA_URL = "/static/"

appid = app_identity.get_application_id()
RIETVELD_INCOMING_MAIL_ADDRESS = "reply@%s.appspotmail.com" % appid
RIETVELD_INCOMING_MAIL_MAX_SIZE = 500 * 1024  # 500K
RIETVELD_REVISION = "<unknown>"
try:
    RIETVELD_REVISION = open(os.path.join(os.path.dirname(__file__), "REVISION")).read()
except:
    pass

UPLOAD_PY_SOURCE = os.path.join(os.path.dirname(__file__), "upload.py")

# Default values for patch rendering
DEFAULT_CONTEXT = 10
DEFAULT_COLUMN_WIDTH = 80
MIN_COLUMN_WIDTH = 3
MAX_COLUMN_WIDTH = 2000
