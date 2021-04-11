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

"""Views for Rietveld."""

import binascii
import calendar
import datetime
import email  # see incoming_mail()
import email.utils
import itertools
import json
import logging
import md5
import os
import random
import re
import tarfile
import tempfile
import time
import urllib.request, urllib.parse, urllib.error
from io import StringIO
from xml.etree import ElementTree

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.datastore import datastore_query
from google.appengine.ext import db
from google.appengine.ext import ndb
from google.appengine.runtime import DeadlineExceededError
from google.appengine.runtime import apiproxy_errors

from django import forms

# Import settings as django_settings to avoid name conflict with settings().
from django.conf import settings as django_settings
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseNotFound
from django.shortcuts import render_to_response
import django.template
from django.template import RequestContext
from django.utils import encoding
from django.utils.safestring import mark_safe
from django.core.urlresolvers import reverse
from django.core.servers.basehttp import FileWrapper

import httplib2
from oauth2client.appengine import _parse_state_value
from oauth2client.appengine import _safe_html
from oauth2client.appengine import CredentialsNDBModel
from oauth2client.appengine import StorageByKeyName
from oauth2client.appengine import xsrf_secret_key
from oauth2client.client import AccessTokenRefreshError
from oauth2client.client import OAuth2WebServerFlow
from oauth2client import xsrfutil

from codereview import auth_utils
from codereview import engine
from codereview import library
from codereview import models
from codereview import patching
from codereview import utils
from codereview.common import IS_DEV
from codereview.exceptions import FetchError
from codereview.responses import HttpTextResponse, HttpHtmlResponse, respond
import codereview.decorators as deco


# Add our own custom template tags library.
django.template.add_to_builtins("codereview.library")


### Constants ###


OAUTH_DEFAULT_ERROR_MESSAGE = "OAuth 2.0 error occurred."
_ACCESS_TOKEN_TEMPLATE_ROOT = "http://localhost:%(port)d?"
ACCESS_TOKEN_REDIRECT_TEMPLATE = _ACCESS_TOKEN_TEMPLATE_ROOT + "access_token=%(token)s"
ACCESS_TOKEN_FAIL_REDIRECT_TEMPLATE = _ACCESS_TOKEN_TEMPLATE_ROOT + "error=%(error)s"
# Maximum forms fields length
MAX_SUBJECT = 100
MAX_DESCRIPTION = 10000
MAX_URL = 2083
MAX_REVIEWERS = 1000
MAX_CC = 2000
MAX_MESSAGE = 10000
MAX_FILENAME = 255
MAX_DB_KEY_LENGTH = 1000

DB_WRITE_TRIES = 3
DB_WRITE_PAUSE = 4


### Form classes ###


class AccountInput(forms.TextInput):
    # Associates the necessary css/js files for the control.  See
    # http://docs.djangoproject.com/en/dev/topics/forms/media/.
    #
    # Don't forget to place {{formname.media}} into html header
    # when using this html control.
    class Media:
        css = {"all": ("autocomplete/jquery.autocomplete.css",)}
        js = (
            "autocomplete/lib/jquery.js",
            "autocomplete/lib/jquery.bgiframe.min.js",
            "autocomplete/lib/jquery.ajaxQueue.js",
            "autocomplete/jquery.autocomplete.js",
        )

    def render(self, name, value, attrs=None):
        output = super(AccountInput, self).render(name, value, attrs)
        if models.Account.current_user_account is not None:
            # TODO(anatoli): move this into .js media for this form
            data = {"name": name, "url": reverse(account), "multiple": "true"}
            if self.attrs.get("multiple", True) == False:
                data["multiple"] = "false"
            output += mark_safe(
                """
      <script type="text/javascript">
          jQuery("#id_%(name)s").autocomplete("%(url)s", {
          max: 10,
          highlight: false,
          multiple: %(multiple)s,
          multipleSeparator: ", ",
          scroll: true,
          scrollHeight: 300,
          matchContains: true,
          formatResult : function(row) {
          return row[0].replace(/ .+/gi, '');
          }
          });
      </script>"""
                % data
            )
        return output


class IssueBaseForm(forms.Form):

    subject = forms.CharField(
        max_length=MAX_SUBJECT, widget=forms.TextInput(attrs={"size": 60})
    )
    description = forms.CharField(
        required=False,
        max_length=MAX_DESCRIPTION,
        widget=forms.Textarea(attrs={"cols": 60}),
    )
    branch = forms.ChoiceField(required=False, label="Base URL")
    base = forms.CharField(
        required=False, max_length=MAX_URL, widget=forms.TextInput(attrs={"size": 60})
    )
    reviewers = forms.CharField(
        required=False,
        max_length=MAX_REVIEWERS,
        widget=AccountInput(attrs={"size": 60}),
    )
    cc = forms.CharField(
        required=False,
        max_length=MAX_CC,
        label="CC",
        widget=AccountInput(attrs={"size": 60}),
    )
    private = forms.BooleanField(required=False, initial=False)

    def get_base(self):
        base = self.cleaned_data.get("base")
        if not base:
            key = self.cleaned_data["branch"]
            if key:
                branch = models.Branch.get_by_id(key)
                if branch is not None:
                    base = branch.url
        if not base:
            self.errors["base"] = ["You must specify a base"]
        return base or None


class UploadForm(forms.Form):

    subject = forms.CharField(max_length=MAX_SUBJECT)
    description = forms.CharField(max_length=MAX_DESCRIPTION, required=False)
    project = forms.CharField(required=False)
    content_upload = forms.BooleanField(required=False)
    separate_patches = forms.BooleanField(required=False)
    base = forms.CharField(max_length=MAX_URL, required=False)
    data = forms.FileField(required=False)
    issue = forms.IntegerField(required=False)
    reviewers = forms.CharField(max_length=MAX_REVIEWERS, required=False)
    cc = forms.CharField(max_length=MAX_CC, required=False)
    private = forms.BooleanField(required=False, initial=False)
    send_mail = forms.BooleanField(required=False)
    base_hashes = forms.CharField(required=False)
    repo_guid = forms.CharField(required=False, max_length=MAX_URL)

    def clean_base(self):
        base = self.cleaned_data.get("base")
        if not base and not self.cleaned_data.get("content_upload", False):
            raise forms.ValidationError("Base URL is required.")
        return self.cleaned_data.get("base")

    def get_base(self):
        return self.cleaned_data.get("base")


class UploadContentForm(forms.Form):
    filename = forms.CharField(max_length=MAX_FILENAME)
    status = forms.CharField(required=False, max_length=20)
    checksum = forms.CharField(max_length=32)
    file_too_large = forms.BooleanField(required=False)
    is_binary = forms.BooleanField(required=False)
    is_current = forms.BooleanField(required=False)

    def clean(self):
        # Check presence of 'data'. We cannot use FileField because
        # it disallows empty files.
        super(UploadContentForm, self).clean()
        if not self.files and "data" not in self.files:
            raise forms.ValidationError("No content uploaded.")
        return self.cleaned_data

    def get_uploaded_content(self):
        return self.files["data"].read()


class UploadPatchForm(forms.Form):
    filename = forms.CharField(max_length=MAX_FILENAME)
    content_upload = forms.BooleanField(required=False)

    def get_uploaded_patch(self):
        return self.files["data"].read()


class EditLocalBaseForm(forms.Form):
    subject = forms.CharField(
        max_length=MAX_SUBJECT, widget=forms.TextInput(attrs={"size": 60})
    )
    description = forms.CharField(
        required=False,
        max_length=MAX_DESCRIPTION,
        widget=forms.Textarea(attrs={"cols": 60}),
    )
    reviewers = forms.CharField(
        required=False,
        max_length=MAX_REVIEWERS,
        widget=AccountInput(attrs={"size": 60}),
    )
    cc = forms.CharField(
        required=False,
        max_length=MAX_CC,
        label="CC",
        widget=AccountInput(attrs={"size": 60}),
    )
    private = forms.BooleanField(
        required=False,
        initial=False,
        label="Protected",
        help_text=(
            "Only viewable by @chromium and @google accounts."
            '<div class="if_checked">'
            "Please, avoid mailing lists in the CC and Reviewers fields."
            "</div>"
        ),
    )
    closed = forms.BooleanField(required=False)

    def get_base(self):
        return None


class RepoForm(forms.Form):
    name = forms.CharField()
    url = forms.URLField()
    guid = forms.CharField(required=False)


class BranchForm(forms.Form):
    category = forms.CharField(
        widget=forms.Select(choices=[(ch, ch) for ch in models.BRANCH_CATEGORY_CHOICES])
    )
    name = forms.CharField()
    url = forms.URLField()


class PublishForm(forms.Form):

    subject = forms.CharField(
        max_length=MAX_SUBJECT, widget=forms.TextInput(attrs={"size": 60})
    )
    reviewers = forms.CharField(
        required=False,
        max_length=MAX_REVIEWERS,
        widget=AccountInput(attrs={"size": 60}),
    )
    cc = forms.CharField(
        required=False,
        max_length=MAX_CC,
        label="CC",
        widget=AccountInput(attrs={"size": 60}),
    )
    send_mail = forms.BooleanField(required=False)
    message = forms.CharField(
        required=False,
        max_length=MAX_MESSAGE,
        widget=forms.Textarea(attrs={"cols": 60}),
    )
    message_only = forms.BooleanField(required=False, widget=forms.HiddenInput())
    no_redirect = forms.BooleanField(required=False, widget=forms.HiddenInput())
    in_reply_to = forms.CharField(
        required=False, max_length=MAX_DB_KEY_LENGTH, widget=forms.HiddenInput()
    )
    automated = forms.BooleanField(
        required=False, widget=forms.HiddenInput(), initial=True
    )
    verbose = forms.BooleanField(required=False, widget=forms.HiddenInput())


class MiniPublishForm(forms.Form):

    reviewers = forms.CharField(
        required=False,
        max_length=MAX_REVIEWERS,
        widget=AccountInput(attrs={"size": 60}),
    )
    cc = forms.CharField(
        required=False,
        max_length=MAX_CC,
        label="CC",
        widget=AccountInput(attrs={"size": 60}),
    )
    send_mail = forms.BooleanField(required=False)
    message = forms.CharField(
        required=False,
        max_length=MAX_MESSAGE,
        widget=forms.Textarea(attrs={"cols": 60}),
    )
    message_only = forms.BooleanField(required=False, widget=forms.HiddenInput())
    no_redirect = forms.BooleanField(required=False, widget=forms.HiddenInput())
    automated = forms.BooleanField(
        required=False, widget=forms.HiddenInput(), initial=True
    )
    verbose = forms.BooleanField(required=False, widget=forms.HiddenInput())


class BlockForm(forms.Form):
    blocked = forms.BooleanField(
        required=False, help_text="Should this user be blocked"
    )


FORM_CONTEXT_VALUES = [(z, "%d lines" % z) for z in models.CONTEXT_CHOICES]
FORM_CONTEXT_VALUES.append(("", "Whole file"))


class SettingsForm(forms.Form):

    nickname = forms.CharField(max_length=30)
    context = forms.IntegerField(
        widget=forms.Select(choices=FORM_CONTEXT_VALUES),
        required=False,
        label="Context",
    )
    column_width = forms.IntegerField(
        initial=django_settings.DEFAULT_COLUMN_WIDTH,
        min_value=django_settings.MIN_COLUMN_WIDTH,
        max_value=django_settings.MAX_COLUMN_WIDTH,
    )
    notify_by_email = forms.BooleanField(required=False, widget=forms.HiddenInput())

    def clean_nickname(self):
        nickname = self.cleaned_data.get("nickname")
        # Check for allowed characters
        match = re.match(r"[\w\.\-_\(\) ]+$", nickname, re.UNICODE | re.IGNORECASE)
        if not match:
            raise forms.ValidationError(
                "Allowed characters are letters, digits, " '".-_()" and spaces.'
            )
        # Check for sane whitespaces
        if re.search(r"\s{2,}", nickname):
            raise forms.ValidationError("Use single spaces between words.")
        if len(nickname) != len(nickname.strip()):
            raise forms.ValidationError(
                "Leading and trailing whitespaces are " "not allowed."
            )

        if nickname.lower() == "me":
            raise forms.ValidationError("Choose a different nickname.")

        # Look for existing nicknames
        query = models.Account.query(models.Account.lower_nickname == nickname.lower())
        if any(
            account.key != models.Account.current_user_account.key for account in query
        ):
            raise forms.ValidationError("This nickname is already in use.")

        return nickname


class MigrateEntitiesForm(forms.Form):

    account = forms.CharField(label="Your previous email address")
    _user = None

    def set_user(self, user):
        """Sets the _user attribute.

    A user object is needed for validation. This method has to be
    called before is_valid() is called to allow us to validate if a
    email address given in account belongs to the same user.
    """
        self._user = user

    def clean_account(self):
        """Verifies that an account with this emails exists and returns it.

    This method is executed by Django when Form.is_valid() is called.
    """
        if self._user is None:
            raise forms.ValidationError("No user given.")
        account = models.Account.get_account_for_email(self.cleaned_data["account"])
        if account is None:
            raise forms.ValidationError("No such email.")
        if account.user.email() == self._user.email():
            raise forms.ValidationError(
                "Nothing to do. This is your current email address."
            )
        if account.user.user_id() != self._user.user_id():
            raise forms.ValidationError(
                "This email address isn't related to your account."
            )
        return account


ORDER_CHOICES = (
    "__key__",
    "owner",
    "created",
    "modified",
)


class SearchForm(forms.Form):

    format = forms.ChoiceField(
        required=False,
        choices=(("html", "html"), ("json", "json")),
        widget=forms.HiddenInput(attrs={"value": "html"}),
    )
    keys_only = forms.BooleanField(
        required=False, widget=forms.HiddenInput(attrs={"value": "False"})
    )
    with_messages = forms.BooleanField(
        required=False, widget=forms.HiddenInput(attrs={"value": "False"})
    )
    cursor = forms.CharField(
        required=False, widget=forms.HiddenInput(attrs={"value": ""})
    )
    limit = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=1000,
        widget=forms.HiddenInput(attrs={"value": "30"}),
    )
    closed = forms.NullBooleanField(required=False)
    owner = forms.CharField(
        required=False,
        max_length=MAX_REVIEWERS,
        widget=AccountInput(attrs={"size": 60, "multiple": False}),
    )
    reviewer = forms.CharField(
        required=False,
        max_length=MAX_REVIEWERS,
        widget=AccountInput(attrs={"size": 60, "multiple": False}),
    )
    cc = forms.CharField(
        required=False,
        max_length=MAX_CC,
        label="CC",
        widget=AccountInput(attrs={"size": 60}),
    )
    repo_guid = forms.CharField(
        required=False, max_length=MAX_URL, label="Repository ID"
    )
    base = forms.CharField(required=False, max_length=MAX_URL)
    private = forms.NullBooleanField(required=False)
    created_before = forms.DateTimeField(
        required=False,
        label="Created before",
        help_text="Format: YYYY-MM-DD and optional: hh:mm:ss",
    )
    created_after = forms.DateTimeField(required=False, label="Created on or after")
    modified_before = forms.DateTimeField(required=False, label="Modified before")
    modified_after = forms.DateTimeField(required=False, label="Modified on or after")
    order = forms.ChoiceField(
        required=False,
        help_text="Order: Name of one of the datastore keys",
        choices=sum(
            ([(x, x), ("-" + x, "-" + x)] for x in ORDER_CHOICES), [("", "(default)")]
        ),
    )

    def _clean_accounts(self, key):
        """Cleans up autocomplete field.

    The input is validated to be zero or one name/email and it's
    validated that the users exists.

    Args:
      key: the field name.

    Returns an User instance or raises ValidationError.
    """
        accounts = [
            _f
            for _f in (x.strip() for x in self.cleaned_data.get(key, "").split(","))
            if _f
        ]
        if len(accounts) > 1:
            raise forms.ValidationError("Only one user name is allowed.")
        elif not accounts:
            return None
        account = accounts[0]
        if "@" in account:
            acct = models.Account.get_account_for_email(account)
        else:
            acct = models.Account.get_account_for_nickname(account)
        if not acct:
            raise forms.ValidationError("Unknown user")
        return acct.user

    def clean_owner(self):
        return self._clean_accounts("owner")

    def clean_reviewer(self):
        user = self._clean_accounts("reviewer")
        if user:
            return user.email()


class StringListField(forms.CharField):
    def prepare_value(self, value):
        if value is None:
            return ""
        return ",".join(value)

    def to_python(self, value):
        if not value:
            return []
        return [list_value.strip() for list_value in value.split(",")]


class ClientIDAndSecretForm(forms.Form):
    """Simple form for collecting Client ID and Secret."""

    client_id = forms.CharField()
    client_secret = forms.CharField()
    additional_client_ids = StringListField()


class UpdateStatsForm(forms.Form):
    tasks_to_trigger = forms.CharField(
        required=True,
        max_length=2000,
        help_text="Coma separated items.",
        widget=forms.TextInput(attrs={"size": "100"}),
    )


### Exceptions ###


class InvalidIncomingEmailError(Exception):
    """Exception raised by incoming mail handler when a problem occurs."""


### Helper functions ###


def _random_bytes(n):
    """Helper returning a string of random bytes of given length."""
    return "".join(map(chr, (random.randrange(256) for i in range(n))))


def _clean_int(value, default, min_value=None, max_value=None):
    """Helper to cast value to int and to clip it to min or max_value.

  Args:
    value: Any value (preferably something that can be casted to int).
    default: Default value to be used when type casting fails.
    min_value: Minimum allowed value (default: None).
    max_value: Maximum allowed value (default: None).

  Returns:
    An integer between min_value and max_value.
  """
    if not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(value, max_value)
    return value


### Request handlers ###


def index(request):
    """/ - Show a list of review issues"""
    if request.user is None:
        return view_all(request, index_call=True)
    else:
        return mine(request)


DEFAULT_LIMIT = 20


def _url(path, **kwargs):
    """Format parameters for query string.

  Args:
    path: Path of URL.
    kwargs: Keyword parameters are treated as values to add to the query
      parameter of the URL.  If empty no query parameters will be added to
      path and '?' omitted from the URL.
  """
    if kwargs:
        encoded_parameters = urllib.parse.urlencode(kwargs)
        if path.endswith("?"):
            # Trailing ? on path.  Append parameters to end.
            return "%s%s" % (path, encoded_parameters)
        elif "?" in path:
            # Append additional parameters to existing query parameters.
            return "%s&%s" % (path, encoded_parameters)
        else:
            # Add query parameters to path with no query parameters.
            return "%s?%s" % (path, encoded_parameters)
    else:
        return path


def _inner_paginate(request, issues, template, extra_template_params):
    """Display paginated list of issues.

  Takes care of the private bit.

  Args:
    request: Request containing offset and limit parameters.
    issues: Issues to be displayed.
    template: Name of template that renders issue page.
    extra_template_params: Dictionary of extra parameters to pass to page
      rendering.

  Returns:
    Response for sending back to browser.
  """
    visible_issues = [i for i in issues if i.view_allowed]
    _optimize_draft_counts(visible_issues)
    _load_users_for_issues(visible_issues)
    params = {
        "issues": visible_issues,
        "limit": None,
        "newest": None,
        "prev": None,
        "next": None,
        "nexttext": "",
        "first": "",
        "last": "",
    }
    if extra_template_params:
        params.update(extra_template_params)
    return respond(request, template, params)


def _paginate_issues(
    page_url,
    request,
    query,
    template,
    extra_nav_parameters=None,
    extra_template_params=None,
):
    """Display paginated list of issues.

  Args:
    page_url: Base URL of issue page that is being paginated.  Typically
      generated by calling 'reverse' with a name and arguments of a view
      function.
    request: Request containing offset and limit parameters.
    query: Query over issues.
    template: Name of template that renders issue page.
    extra_nav_parameters: Dictionary of extra parameters to append to the
      navigation links.
    extra_template_params: Dictionary of extra parameters to pass to page
      rendering.

  Returns:
    Response for sending back to browser.
  """
    offset = _clean_int(request.GET.get("offset"), 0, 0)
    limit = _clean_int(request.GET.get("limit"), DEFAULT_LIMIT, 1, 100)

    nav_parameters = {"limit": str(limit)}
    if extra_nav_parameters is not None:
        nav_parameters.update(extra_nav_parameters)

    params = {
        "limit": limit,
        "first": offset + 1,
        "nexttext": "Older",
    }
    # Fetch one more to see if there should be a 'next' link
    logging.info("query during pagination is %r", query)
    issues = query.fetch(limit + 1, offset=offset)
    if len(issues) > limit:
        del issues[limit:]
        params["next"] = _url(page_url, offset=offset + limit, **nav_parameters)
    params["last"] = len(issues) > 1 and offset + len(issues) or None
    if offset > 0:
        params["prev"] = _url(page_url, offset=max(0, offset - limit), **nav_parameters)
    if offset > limit:
        params["newest"] = _url(page_url, **nav_parameters)
    if extra_template_params:
        params.update(extra_template_params)
    return _inner_paginate(request, issues, template, params)


def _paginate_issues_with_cursor(
    page_url,
    request,
    query,
    cursor,
    limit,
    template,
    extra_nav_parameters=None,
    extra_template_params=None,
):
    """Display paginated list of issues using a cursor instead of offset.

  Args:
    page_url: Base URL of issue page that is being paginated.  Typically
      generated by calling 'reverse' with a name and arguments of a view
      function.
    request: Request containing offset and limit parameters.
    query: Query over issues
    cursor: cursor object passed to web form and back again.
    limit: Maximum number of issues to return.
    template: Name of template that renders issue page.
    extra_nav_parameters: Dictionary of extra parameters to append to the
      navigation links.
    extra_template_params: Dictionary of extra parameters to pass to page
      rendering.

  Returns:
    Response for sending back to browser.
  """
    issues, next_cursor, has_more = query.fetch_page(limit, start_cursor=cursor)
    nav_parameters = {}
    if extra_nav_parameters:
        nav_parameters.update(extra_nav_parameters)
    nav_parameters["cursor"] = next_cursor.urlsafe() if next_cursor else ""

    params = {
        "limit": limit,
        "cursor": nav_parameters["cursor"],
        "nexttext": "Next",
    }
    if has_more:
        params["next"] = _url(page_url, **nav_parameters)
    if extra_template_params:
        params.update(extra_template_params)
    return _inner_paginate(request, issues, template, params)


def view_all(request, index_call=False):
    """/all - Show a list of up to DEFAULT_LIMIT recent issues."""
    closed = request.GET.get("closed", "")
    if closed in ("0", "false"):
        closed = False
    elif closed in ("1", "true"):
        closed = True
    elif index_call:
        # for index we display only open issues by default
        closed = False
    else:
        closed = None

    nav_parameters = {}
    if closed is not None:
        nav_parameters["closed"] = int(closed)

    query = models.Issue.query(models.Issue.private == False).order(
        -models.Issue.modified
    )
    if closed is not None:
        # return only opened or closed issues
        query = query.filter(models.Issue.closed == closed)

    return _paginate_issues(
        reverse(view_all),
        request,
        query,
        "all.html",
        extra_nav_parameters=nav_parameters,
        extra_template_params=dict(closed=closed),
    )


def _optimize_draft_counts(issues):
    """Force _num_drafts to zero for issues that are known to have no drafts.

  Args:
    issues: list of model.Issue instances.

  This inspects the drafts attribute of the current user's Account
  instance, and forces the draft count to zero of those issues in the
  list that aren't mentioned there.

  If there is no current user, all draft counts are forced to 0.
  """
    account = models.Account.current_user_account
    if account is None:
        issue_ids = None
    else:
        issue_ids = account.drafts
    for issue in issues:
        if issue_ids is None or issue.key.id() not in issue_ids:
            issue._num_drafts = issue._num_drafts or {}
            if account:
                issue._num_drafts[account.email] = 0


@deco.login_required
def mine(request):
    """/mine - Show a list of issues created by the current user."""
    request.user_to_show = request.user
    return _show_user(request)


@deco.login_required
def starred(request):
    """/starred - Show a list of issues starred by the current user."""
    stars = models.Account.current_user_account.stars
    if not stars:
        issues = []
    else:
        starred_issue_keys = [ndb.Key(models.Issue, i) for i in stars]
        issues = [
            issue
            for issue in ndb.get_multi(starred_issue_keys)
            if issue and issue.view_allowed
        ]
        _load_users_for_issues(issues)
        _optimize_draft_counts(issues)
    return respond(request, "starred.html", {"issues": issues})


def _load_users_for_issues(issues):
    """Load all user links for a list of issues in one go."""
    user_dict = {}
    for i in issues:
        for e in i.reviewers + i.cc + [i.owner.email()]:
            # keeping a count lets you track total vs. distinct if you want
            user_dict[e] = user_dict.setdefault(e, 0) + 1

    library.get_links_for_users(list(user_dict.keys()))


@deco.user_key_required
def show_user(request):
    """/user - Show the user's dashboard"""
    return _show_user(request)


def _show_user(request):
    user = request.user_to_show
    if user == request.user:
        draft_query = models.Comment.query(
            models.Comment.draft == True, models.Comment.author == request.user
        )
        draft_issue_keys = {
            draft_key.parent().parent().parent()
            for draft_key in draft_query.fetch(100, keys_only=True)
        }
        draft_issues = ndb.get_multi(draft_issue_keys)
        # Reduce the chance of someone trying to block himself.
        show_block = False
    else:
        draft_issues = draft_issue_keys = []
        show_block = request.user_is_admin
    my_issues = [
        issue
        for issue in models.Issue.query(
            models.Issue.closed == False, models.Issue.owner == user
        )
        .order(-models.Issue.modified)
        .fetch(100)
        if issue.key not in draft_issue_keys and issue.view_allowed
    ]
    review_issues = [
        issue
        for issue in models.Issue.query(
            models.Issue.closed == False, models.Issue.reviewers == user.email().lower()
        )
        .order(-models.Issue.modified)
        .fetch(100)
        if (
            issue.key not in draft_issue_keys
            and issue.owner != user
            and issue.view_allowed
        )
    ]
    earliest_closed = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    closed_issues = [
        issue
        for issue in models.Issue.query(
            models.Issue.closed == True,
            models.Issue.modified > earliest_closed,
            models.Issue.owner == user,
        )
        .order(-models.Issue.modified)
        .fetch(100)
        if issue.key not in draft_issue_keys and issue.view_allowed
    ]
    cc_issues = [
        issue
        for issue in models.Issue.query(
            models.Issue.closed == False, models.Issue.cc == user.email()
        )
        .order(-models.Issue.modified)
        .fetch(100)
        if (
            issue.key not in draft_issue_keys
            and issue.owner != user
            and issue.view_allowed
        )
    ]
    all_issues = my_issues + review_issues + closed_issues + cc_issues

    # Some of these issues may not have accurate updates_for information,
    # so ask each issue to update itself.
    futures = []
    for issue in itertools.chain(draft_issues, all_issues):
        ret = issue.calculate_and_save_updates_if_None()
        if ret is not None:
            futures.append(ret)
    for f in futures:
        f.get_result()

    # When a CL is sent from upload.py using --send_mail we create an empty
    # message. This might change in the future, either by not adding an empty
    # message or by populating the message with the content of the email
    # that was sent out.
    outgoing_issues = [issue for issue in my_issues if issue.num_messages]
    unsent_issues = [issue for issue in my_issues if not issue.num_messages]
    _load_users_for_issues(all_issues)
    _optimize_draft_counts(all_issues)
    account = models.Account.get_account_for_user(request.user_to_show)
    return respond(
        request,
        "user.html",
        {
            "viewed_account": account,
            "outgoing_issues": outgoing_issues,
            "unsent_issues": unsent_issues,
            "review_issues": review_issues,
            "closed_issues": closed_issues,
            "cc_issues": cc_issues,
            "draft_issues": draft_issues,
            "show_block": show_block,
        },
    )


@deco.require_methods("POST")
@deco.login_required
@deco.patchset_required
@deco.xsrf_required
def edit_patchset_title(request):
    """/<issue>/edit_patchset_title - Edit the specified patchset's title."""

    if request.user.email().lower() != request.issue.owner.email():
        return HttpResponseBadRequest("Only the issue owner can edit patchset titles")

    patchset = request.patchset
    patchset.message = request.POST.get("patchset_title")
    patchset.put()

    return HttpResponse("OK", content_type="text/plain")


@deco.admin_required
@deco.user_key_required
@deco.xsrf_required
def block_user(request):
    """/user/<user>/block - Blocks a specific user."""
    account = models.Account.get_account_for_user(request.user_to_show)
    if request.method == "POST":
        form = BlockForm(request.POST)
        if form.is_valid():
            account.blocked = form.cleaned_data["blocked"]
            logging.debug(
                "Updating block bit to %s for user %s", account.blocked, account.email
            )
            account.put()
            if account.blocked:
                # Remove user from existing issues so that he doesn't participate in
                # email communication anymore.
                tbd = {}
                email = account.user.email()
                query = models.Issue.query(models.Issue.reviewers == email)
                for issue in query:
                    issue.reviewers.remove(email)
                    issue.calculate_updates_for()
                    tbd[issue.key] = issue
                # look for issues where blocked user is in cc only
                query = models.Issue.query(models.Issue.cc == email)
                for issue in query:
                    if issue.key in tbd:
                        # Update already changed instance instead. This happens when the
                        # blocked user is in both reviewers and ccs.
                        issue = tbd[issue.key]
                    issue.cc.remove(account.user.email())
                    tbd[issue.key] = issue
                ndb.put_multi(list(tbd.values()))
    else:
        form = BlockForm()
    form.initial["blocked"] = account.blocked
    templates = {
        "viewed_account": account,
        "form": form,
    }
    return respond(request, "block_user.html", templates)


@deco.login_required
@deco.xsrf_required
def use_uploadpy(request):
    """Show an intermediate page about upload.py."""
    if request.method == "POST":
        return HttpResponseRedirect(reverse(customized_upload_py))
    return respond(request, "use_uploadpy.html")


@deco.require_methods("POST")
@deco.upload_required
def upload(request):
    """/upload - Used by upload.py to create a new Issue and add PatchSet's to
  existing Issues.

  This generates a text/plain response.
  """
    if request.user is None:
        if IS_DEV:
            request.user = users.User(request.POST.get("user", "test@example.com"))
        else:
            return HttpTextResponse("Login required", status=401)
    # Check against old upload.py usage.
    if request.POST.get("num_parts") > 1:
        return HttpTextResponse("Upload.py is too old, get the latest version.")
    form = UploadForm(request.POST, request.FILES)
    issue = None
    patchset = None
    if form.is_valid():
        issue_id = form.cleaned_data["issue"]
        if issue_id:
            action = "updated"
            issue = models.Issue.get_by_id(issue_id)
            if issue is None:
                form.errors["issue"] = ["No issue exists with that id (%s)" % issue_id]
            elif not form.cleaned_data.get("content_upload"):
                form.errors["issue"] = ["Base files upload required for that issue."]
                issue = None
            else:
                if not issue.edit_allowed:
                    form.errors["user"] = [
                        "You (%s) don't own this issue (%s)" % (request.user, issue_id)
                    ]
                    issue = None
                elif issue.closed:
                    form.errors["issue"] = ["This issue is closed (%s)" % (issue_id)]
                    issue = None
                else:
                    patchset = _add_patchset_from_form(
                        request, issue, form, "subject", emails_add_only=True
                    )
                    if not patchset:
                        issue = None
        else:
            action = "created"
            issue, patchset = _make_new(request, form)
    if issue is None:
        msg = "Issue creation errors: %s" % repr(form.errors)
    else:
        msg = "Issue %s. URL: %s" % (
            action,
            request.build_absolute_uri(
                reverse("show_bare_issue_number", args=[issue.key.id()])
            ),
        )
        if form.cleaned_data.get("content_upload") or form.cleaned_data.get(
            "separate_patches"
        ):
            # Extend the response message: 2nd line is patchset id.
            msg += "\n%d" % patchset.key.id()
            if form.cleaned_data.get("content_upload"):
                # Extend the response: additional lines are the expected filenames.
                issue.put()

                base_hashes = {}
                for file_info in form.cleaned_data.get("base_hashes").split("|"):
                    if not file_info:
                        break
                    checksum, filename = file_info.split(":", 1)
                    base_hashes[filename] = checksum

                logging.info("base_hashes is %r", base_hashes)
                content_entities = []
                new_content_entities = []
                patches = list(patchset.patches)
                logging.info("len(patches) = %r", len(patches))

                existing_patches = {}
                patchsets = list(issue.patchsets)
                if len(patchsets) > 1:
                    # Only check the last uploaded patchset for speed.
                    last_patch_list = patchsets[-2].patches
                    patchsets = None  # Reduce memory usage.
                    for opatch in last_patch_list:
                        if opatch.content_key:
                            existing_patches[opatch.filename] = opatch
                for patch in patches:
                    # Check if the base file is already uploaded in another patchset.
                    if (
                        patch.filename in base_hashes
                        and patch.filename in existing_patches
                        and (
                            base_hashes[patch.filename]
                            == existing_patches[patch.filename]
                            .content_key.get()
                            .checksum
                        )
                    ):
                        content_key = existing_patches[patch.filename].content_key
                        patch.status = existing_patches[patch.filename].status
                        patch.is_binary = existing_patches[patch.filename].is_binary
                        patch.content_key = content_key

                existing_patches = None  # Reduce memory usage.

                for patch in patches:
                    id_string = patch.key.id()
                    if patch.content_key is not None:
                        # Base file not needed since we reused a previous upload.  Send its
                        # patch id in case it's a binary file and the new content needs to
                        # be uploaded.  We mark this by prepending 'nobase' to the id.
                        id_string = "nobase_" + str(id_string)
                    msg += "\n%s %s" % (id_string, patch.filename)

                logging.info("upload response is:\n %s\n", msg)
                ndb.put_multi(patches)

    return HttpTextResponse(msg)


@ndb.transactional()
def _update_patch(patch_key, content_key, is_current, status, is_binary):
    """Store content-related info in a Patch."""
    patch = patch_key.get()
    patch.status = status
    patch.is_binary = is_binary
    if is_current:
        patch.patched_content_key = content_key
    else:
        patch.content_key = content_key
    patch.put()


@deco.require_methods("POST")
@deco.patch_required
@deco.upload_required
def upload_content(request):
    """/<issue>/upload_content/<patchset>/<patch> - Upload base file contents.

  Used by upload.py to upload base files.
  """
    form = UploadContentForm(request.POST, request.FILES)
    if not form.is_valid():
        return HttpTextResponse("ERROR: Upload content errors:\n%s" % repr(form.errors))
    if request.user is None:
        if IS_DEV:
            request.user = users.User(request.POST.get("user", "test@example.com"))
        else:
            return HttpTextResponse("Error: Login required", status=401)
    if not request.issue.edit_allowed:
        return HttpTextResponse(
            "ERROR: You (%s) don't own this issue (%s)."
            % (request.user, request.issue.key.id())
        )
    patch = request.patch

    if form.cleaned_data["is_current"]:
        if patch.patched_content_key:
            return HttpTextResponse("ERROR: Already have current content.")
    else:
        if patch.content_key:
            return HttpTextResponse("ERROR: Already have base content.")

    content = models.Content(is_uploaded=True, parent=patch.key)

    if form.cleaned_data["file_too_large"]:
        content.file_too_large = True
    else:
        data = form.get_uploaded_content()
        checksum = md5.new(data).hexdigest()
        if checksum != request.POST.get("checksum"):
            return HttpTextResponse("ERROR: Checksum mismatch.")
        if form.cleaned_data["is_binary"]:
            content.data = data
        else:
            content.text = utils.to_dbtext(utils.unify_linebreaks(data))
        content.checksum = checksum

    for try_number in range(DB_WRITE_TRIES):
        try:
            content.put()
            _update_patch(
                patch.key,
                content.key,
                form.cleaned_data["is_current"],
                form.cleaned_data["status"],
                form.cleaned_data["is_binary"],
            )
            return HttpTextResponse("OK")
        except db.TransactionFailedError as err:
            if not err.message.endswith("Please try again."):
                logging.exception(err)
            # AppEngine datastore cannot write to the same entity group rapidly.
            time.sleep(DB_WRITE_PAUSE + try_number * random.random())

    return HttpTextResponse("Error: could not store data", status=500)


@deco.require_methods("POST")
@deco.patchset_required
@deco.upload_required
def upload_patch(request):
    """/<issue>/upload_patch/<patchset> - Upload patch to patchset.

  Used by upload.py to upload a patch when the diff is too large to upload all
  together.
  """
    if request.user is None:
        if IS_DEV:
            request.user = users.User(request.POST.get("user", "test@example.com"))
        else:
            return HttpTextResponse("Error: Login required", status=401)
    if not request.issue.edit_allowed:
        return HttpTextResponse(
            "ERROR: You (%s) don't own this issue (%s)."
            % (request.user, request.issue.key.id())
        )
    form = UploadPatchForm(request.POST, request.FILES)
    if not form.is_valid():
        return HttpTextResponse("ERROR: Upload patch errors:\n%s" % repr(form.errors))
    patchset = request.patchset
    if patchset.data:
        return HttpTextResponse("ERROR: Can't upload patches to patchset with data.")
    text = utils.to_dbtext(utils.unify_linebreaks(form.get_uploaded_patch()))
    patch = models.Patch(
        patchset_key=patchset.key,
        text=text,
        filename=form.cleaned_data["filename"],
        parent=patchset.key,
    )
    patch.put()

    msg = "OK\n" + str(patch.key.id())
    return HttpTextResponse(msg)


@deco.require_methods("POST")
@deco.issue_editor_required
@deco.upload_required
def upload_complete(request, patchset_id=None):
    """/<issue>/upload_complete/<patchset> - Patchset upload is complete.
     /<issue>/upload_complete/ - used when no base files are uploaded.

  The following POST parameters are handled:

   - send_mail: If 'yes', a notification mail will be send.
   - attach_patch: If 'yes', the patches will be attached to the mail.
  """
    if patchset_id is not None:
        patchset = models.PatchSet.get_by_id(int(patchset_id), parent=request.issue.key)
        if patchset is None:
            return HttpTextResponse(
                "No patch set exists with that id (%s)" % patchset_id, status=403
            )
        # Add delta calculation task.
        # TODO(jrobbins): If this task has transient failures, consider using cron.
        taskqueue.add(
            url=reverse(task_calculate_delta),
            params={"key": patchset.key.urlsafe()},
            queue_name="deltacalculation",
        )
    else:
        patchset = None
    # Check for completeness
    errors = []
    if patchset is not None:
        query = models.Patch.query(
            models.Patch.is_binary == False,
            models.Patch.status == None,
            ancestor=patchset.key,
        )
        # All uploaded files have a status, any with status==None are missing.
        if query.count() > 0:
            errors.append("Base files missing.")

    if errors:
        msg = (
            "The following errors occured:\n%s\n"
            "Try to upload the changeset again." % "\n".join(errors)
        )
        logging.error("Returning error:\n %s", msg)
        return HttpTextResponse(msg, status=500)

    # Create (and send) a message if needed.
    if request.POST.get("send_mail") == "yes" or request.POST.get("message"):
        msg = _make_message(
            request,
            request.issue,
            request.POST.get("message", ""),
            send_mail=(request.POST.get("send_mail", "") == "yes"),
        )
        request.issue.put()
        msg.put()

    return HttpTextResponse("OK")


def _make_new(request, form):
    """Creates new issue and fill relevant fields from given form data.

  Sends notification about created issue (if requested with send_mail param).

  Returns (Issue, PatchSet) or (None, None).
  """
    if not form.is_valid():
        return (None, None)
    account = models.Account.get_account_for_user(request.user)
    if account.blocked:
        # Early exit for blocked accounts.
        return (None, None)

    data_url = _get_data_url(form)
    if data_url is None:
        return (None, None)
    data, url, separate_patches = data_url

    reviewers = _get_emails(form, "reviewers")
    if not form.is_valid() or reviewers is None:
        return (None, None)

    cc = _get_emails(form, "cc")
    if not form.is_valid():
        return (None, None)

    base = form.get_base()
    if base is None:
        return (None, None)

    first_issue_id, _ = models.Issue.allocate_ids(1)
    issue_key = ndb.Key(models.Issue, first_issue_id)

    issue = models.Issue(
        subject=form.cleaned_data["subject"],
        description=form.cleaned_data["description"],
        project=form.cleaned_data["project"],
        base=base,
        repo_guid=form.cleaned_data.get("repo_guid", None),
        reviewers=reviewers,
        cc=cc,
        private=form.cleaned_data.get("private", False),
        n_comments=0,
        key=issue_key,
    )
    issue.put()

    first_ps_id, _ = models.PatchSet.allocate_ids(1, parent=issue.key)
    ps_key = ndb.Key(models.PatchSet, first_ps_id, parent=issue.key)
    patchset = models.PatchSet(issue_key=issue.key, data=data, url=url, key=ps_key)
    patchset.put()

    if not separate_patches:
        try:
            patches = engine.ParsePatchSet(patchset)
        except:
            # catch all exceptions happening in engine.ParsePatchSet,
            # engine.SplitPatch. With malformed diffs a variety of exceptions could
            # happen there.
            logging.exception("Exception during patch parsing")
            patches = []
        if not patches:
            patchset.key.delete()
            issue.key.delete()
            errkey = url and "url" or "data"
            form.errors[errkey] = ["Patch set contains no recognizable patches"]
            return (None, None)

        ndb.put_multi(patches)

    if form.cleaned_data.get("send_mail"):
        msg = _make_message(request, issue, "", "", True)
        issue.put()
        msg.put()
    return (issue, patchset)


def _get_data_url(form):
    """Helper for _make_new().

  Args:
    form: Django form object.

  Returns:
    3-tuple (data, url, separate_patches).
      data: the diff content, if available.
      url: the url of the diff, if given.
      separate_patches: True iff the patches will be uploaded separately for
        each file.

  """
    cleaned_data = form.cleaned_data

    data = cleaned_data["data"]
    url = cleaned_data.get("url")
    separate_patches = cleaned_data.get("separate_patches")
    if not (data or url or separate_patches):
        form.errors["data"] = ["You must specify a URL or upload a file (< 1 MB)."]
        return None
    if data and url:
        form.errors["data"] = [
            "You must specify either a URL or upload a file " "but not both."
        ]
        return None
    if separate_patches and (data or url):
        form.errors["data"] = [
            "If the patches will be uploaded separately later, "
            "you can't send some data or a url."
        ]
        return None

    if data is not None:
        data = db.Blob(utils.unify_linebreaks(data.read()))
        url = None
    elif url:
        try:
            fetch_result = urlfetch.fetch(url, validate_certificate=True)
        except Exception as err:
            form.errors["url"] = [str(err)]
            return None
        if fetch_result.status_code != 200:
            form.errors["url"] = ["HTTP status code %s" % fetch_result.status_code]
            return None
        data = db.Blob(utils.unify_linebreaks(fetch_result.content))

    return data, url, separate_patches


def _add_patchset_from_form(
    request, issue, form, message_key="message", emails_add_only=False
):
    """Helper for upload()."""
    if form.is_valid():
        data_url = _get_data_url(form)
    if not form.is_valid():
        return None
    account = models.Account.get_account_for_user(request.user)
    if account.blocked:
        return None
    if not issue.edit_allowed:
        # This check is done at each call site but check again as a safety measure.
        return None
    data, url, separate_patches = data_url
    message = form.cleaned_data[message_key]
    first_id, _ = models.PatchSet.allocate_ids(1, parent=issue.key)
    ps_key = ndb.Key(models.PatchSet, first_id, parent=issue.key)
    patchset = models.PatchSet(
        issue_key=issue.key, message=message, data=data, url=url, key=ps_key
    )
    patchset.put()

    if not separate_patches:
        try:
            patches = engine.ParsePatchSet(patchset)
        except:
            logging.exception("Exception during patchset parsing")
            patches = []
        if not patches:
            patchset.key.delete()
            errkey = url and "url" or "data"
            form.errors[errkey] = ["Patch set contains no recognizable patches"]
            return None
        ndb.put_multi(patches)

    if emails_add_only:
        emails = _get_emails(form, "reviewers")
        if not form.is_valid():
            return None
        issue.reviewers += [
            reviewer for reviewer in emails if reviewer not in issue.reviewers
        ]
        emails = _get_emails(form, "cc")
        if not form.is_valid():
            return None
        issue.cc += [cc for cc in emails if cc not in issue.cc]
    else:
        issue.reviewers = _get_emails(form, "reviewers")
        issue.cc = _get_emails(form, "cc")
    issue.calculate_updates_for()
    issue.put()

    if form.cleaned_data.get("send_mail"):
        msg = _make_message(request, issue, message, "", True)
        issue.put()
        msg.put()
    return patchset


def _get_emails(form, label):
    """Helper to return the list of reviewers, or None for error."""
    raw_emails = form.cleaned_data.get(label)
    if raw_emails:
        return _get_emails_from_raw(raw_emails.split(","), form=form, label=label)
    return []


def _get_emails_from_raw(raw_emails, form=None, label=None):
    emails = []
    for email in raw_emails:
        email = email.strip()
        if email:
            try:
                if "@" not in email:
                    account = models.Account.get_account_for_nickname(email)
                    if account is None:
                        raise db.BadValueError("Unknown user: %s" % email)
                    db_email = account.user.email().lower()
                elif email.count("@") != 1:
                    raise db.BadValueError("Invalid email address: %s" % email)
                else:
                    _, tail = email.split("@")
                    if "." not in tail:
                        raise db.BadValueError("Invalid email address: %s" % email)
                    db_email = email.lower()
            except db.BadValueError as err:
                if form:
                    form.errors[label] = [str(err)]
                return None
            if db_email not in emails:
                emails.append(db_email)
    # Remove blocked accounts
    for account in list(models.Account.get_multiple_accounts_by_email(emails).values()):
        if account.blocked:
            try:
                emails.remove(account.email)
            except IndexError:
                pass
    return emails


@deco.issue_required
def show(request):
    """/<issue> - Show an issue."""
    patchsets = request.issue.get_patchset_info(request.user, None)
    last_patchset = first_patch = None
    if patchsets:
        last_patchset = patchsets[-1]
        if last_patchset.patches:
            first_patch = last_patchset.patches[0]
    messages = []
    has_draft_message = False
    for msg in request.issue.messages:
        if not msg.draft:
            messages.append(msg)
        elif msg.draft and request.user and msg.sender == request.user.email():
            has_draft_message = True
    num_patchsets = len(patchsets)
    return respond(
        request,
        "issue.html",
        {
            "first_patch": first_patch,
            "has_draft_message": has_draft_message,
            "is_editor": request.issue.edit_allowed,
            "issue": request.issue,
            "last_patchset": last_patchset,
            "messages": messages,
            "num_patchsets": num_patchsets,
            "patchsets": patchsets,
        },
    )


@deco.patchset_required
def patchset(request):
    """/patchset/<key> - Returns patchset information."""
    patchsets = request.issue.get_patchset_info(request.user, request.patchset.key.id())
    for ps in patchsets:
        if ps.key.id() == request.patchset.key.id():
            patchset = ps
    return respond(
        request,
        "patchset.html",
        {
            "issue": request.issue,
            "patchset": request.patchset,
            "patchsets": patchsets,
            "is_editor": request.issue.edit_allowed,
        },
    )


@deco.login_required
def account(request):
    """/account/?q=blah&limit=10&timestamp=blah - Used for autocomplete."""

    def searchAccounts(prop, domain, added, response):
        prefix = request.GET.get("q").lower()
        limit = _clean_int(request.GET.get("limit"), 10, 10, 100)

        accounts_query = models.Account.query(
            prop >= prefix, prop < prefix + "\ufffd"
        ).order(prop)
        for account in accounts_query:
            if account.blocked:
                continue
            if account.key in added:
                continue
            if domain and not account.email.endswith(domain):
                continue
            if len(added) >= limit:
                break
            added.add(account.key)
            response += "%s (%s)\n" % (account.email, account.nickname)
        return added, response

    added = set()
    response = ""
    domain = os.environ["AUTH_DOMAIN"]
    if domain != "gmail.com":
        # 'gmail.com' is the value AUTH_DOMAIN is set to if the app is running
        # on appspot.com and shouldn't prioritize the custom domain.
        added, response = searchAccounts(
            models.Account.lower_email, domain, added, response
        )
        added, response = searchAccounts(
            models.Account.lower_nickname, domain, added, response
        )

    added, response = searchAccounts(models.Account.lower_nickname, "", added, response)
    added, response = searchAccounts(models.Account.lower_email, "", added, response)
    return HttpTextResponse(response)


@deco.issue_editor_required
@deco.xsrf_required
def edit(request):
    """/<issue>/edit - Edit an issue."""
    issue = request.issue
    base = issue.base

    if request.method != "POST":
        reviewers = [
            models.Account.get_nickname_for_email(reviewer, default=reviewer)
            for reviewer in issue.reviewers
        ]
        ccs = [models.Account.get_nickname_for_email(cc, default=cc) for cc in issue.cc]
        form = EditLocalBaseForm(
            initial={
                "subject": issue.subject,
                "description": issue.description,
                "base": base,
                "reviewers": ", ".join(reviewers),
                "cc": ", ".join(ccs),
                "closed": issue.closed,
                "private": issue.private,
            }
        )
        return respond(
            request,
            "edit.html",
            {
                "issue": issue,
                "form": form,
                "offer_delete": (
                    issue.owner == request.user or auth_utils.is_current_user_admin()
                ),
            },
        )

    form = EditLocalBaseForm(request.POST)

    if form.is_valid():
        reviewers = _get_emails(form, "reviewers")

    if form.is_valid():
        cc = _get_emails(form, "cc")

    if not form.is_valid():
        return respond(request, "edit.html", {"issue": issue, "form": form})
    cleaned_data = form.cleaned_data

    was_closed = issue.closed
    issue.subject = cleaned_data["subject"]
    issue.description = cleaned_data["description"]
    issue.closed = cleaned_data["closed"]
    issue.private = cleaned_data.get("private", False)
    base_changed = issue.base != base
    issue.base = base
    issue.reviewers = reviewers
    issue.cc = cc
    if base_changed:
        for patchset in issue.patchsets:
            ndb.transaction(lambda: _delete_cached_contents(list(patchset.patches)))
    issue.calculate_updates_for()
    issue.put()

    return HttpResponseRedirect(reverse(show, args=[issue.key.id()]))


def _delete_cached_contents(patch_list):
    """Transactional helper for edit() to delete cached contents."""
    # TODO(guido): No need to do this in a transaction.
    patches = []
    content_keys = []
    for patch in patch_list:
        try:
            content_key = patch.content_key
        except db.Error:
            content_key = None
        try:
            patched_content_key = patch.patched_content_key
        except db.Error:
            patched_content_key = None
        if content_key is not None:
            content_keys.append(content_key)
        if patched_content_key is not None:
            content_keys.append(patched_content_key)
        patch.content_key = None
        patch.patched_content_key = None
        patches.append(patch)
    if content_keys:
        logging.info("Deleting %d contents", len(content_keys))
        ndb.delete_multi(content_keys)
    if patches:
        logging.info("Updating %d patches", len(patches))
        ndb.put_multi(patches)


@deco.require_methods("POST")
@deco.issue_editor_required
@deco.xsrf_required
def delete(request):
    """/<issue>/delete - Delete an issue.  There is no way back."""
    issue = request.issue
    tbd = [issue]
    for cls in [
        models.PatchSet,
        models.Patch,
        models.Comment,
        models.Message,
        models.Content,
    ]:
        tbd += cls.query(ancestor=issue.key)
    ndb.delete_multi(entity.key for entity in tbd)
    return HttpResponseRedirect(reverse(mine))


@deco.require_methods("POST")
@deco.patchset_editor_required
@deco.xsrf_required
def delete_patchset(request):
    """/<issue>/patch/<patchset>/delete - Delete a patchset.

  There is no way back.
  """
    request.patchset.nuke()
    return HttpResponseRedirect(reverse(show, args=[request.issue.key.id()]))


@deco.require_methods("POST")
@deco.issue_editor_required
@deco.xsrf_required
def close(request):
    """/<issue>/close - Close an issue."""
    issue = request.issue
    issue.closed = True
    if request.method == "POST":
        new_description = request.POST.get("description")
        if new_description:
            issue.description = new_description
    issue.put()
    return HttpTextResponse("Closed")


@deco.require_methods("POST")
@deco.issue_required
@deco.upload_required
def mailissue(request):
    """/<issue>/mail - Send mail for an issue.

  This URL is deprecated and shouldn't be used anymore.  However,
  older versions of upload.py or wrapper scripts still may use it.
  """
    if not request.issue.edit_allowed:
        if not IS_DEV:
            return HttpTextResponse("Login required", status=401)
    issue = request.issue
    msg = _make_message(request, issue, "", "", True)
    issue.put()
    msg.put()

    return HttpTextResponse("OK")


@deco.access_control_allow_origin_star
@deco.patchset_required
def download(request):
    """/download/<issue>_<patchset>.diff - Download a patch set."""
    if request.patchset.data is None:
        return HttpTextResponse(
            "Patch set (%s) is too large." % request.patchset.key.id(), status=404
        )
    padding = ""
    user_agent = request.META.get("HTTP_USER_AGENT")
    if user_agent and "MSIE" in user_agent:
        # Add 256+ bytes of padding to prevent XSS attacks on Internet Explorer.
        padding = ("=" * 67 + "\n") * 4
    return HttpTextResponse(padding + request.patchset.data)


@deco.patchset_required
def tarball(request):
    """/tarball/<issue>/<patchset>/[lr] - Returns a .tar.bz2 file
  containing a/ and b/ trees of the complete files for the entire patchset."""

    patches = (
        models.Patch.query(models.Patch.patchset_key == request.patchset.key)
        .order(models.Patch.filename)
        .fetch(1000)
    )

    temp = tempfile.TemporaryFile()
    tar = tarfile.open(mode="w|bz2", fileobj=temp)

    def add_entry(prefix, content):
        data = content.data
        if data is None:
            data = content.text
            if isinstance(data, str):
                data = data.encode("utf-8", "replace")
        if data is None:
            return
        info = tarfile.TarInfo(prefix + patch.filename)
        info.size = len(data)
        # TODO(adonovan): set SYMTYPE/0755 when Rietveld supports symlinks.
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        # datetime->time_t
        delta = request.patchset.modified - datetime.datetime(1970, 1, 1)
        info.mtime = int(delta.days * 86400 + delta.seconds)
        tar.addfile(info, fileobj=StringIO(data))

    for patch in patches:
        if not patch.no_base_file:
            try:
                add_entry("a/", patch.get_content())  # before
            except FetchError:  # I/O problem?
                logging.exception(
                    "tarball: patch(%s, %s).get_content failed"
                    % (patch.key.id(), patch.filename)
                )
        try:
            add_entry("b/", patch.get_patched_content())  # after
        except FetchError:  # file deletion?  I/O problem?
            logging.exception(
                "tarball: patch(%s, %s).get_patched_content failed"
                % (patch.key.id(), patch.filename)
            )

    tar.close()
    temp.flush()

    wrapper = FileWrapper(temp)
    response = HttpResponse(wrapper, mimetype="application/x-gtar")
    response["Content-Disposition"] = "attachment; filename=patch%s_%s.tar.bz2" % (
        request.issue.key.id(),
        request.patchset.key.id(),
    )
    response["Content-Length"] = temp.tell()
    temp.seek(0)
    return response


@deco.issue_required
@deco.upload_required
def description(request):
    """/<issue>/description - Gets/Sets an issue's description.

  Used by upload.py or similar scripts.
  """
    if request.method != "POST":
        description = request.issue.description or ""
        return HttpTextResponse(description)
    if not request.issue.edit_allowed:
        if not IS_DEV:
            return HttpTextResponse("Login required", status=401)
    issue = request.issue
    issue.description = request.POST.get("description")
    issue.put()
    return HttpTextResponse("")


@deco.issue_required
@deco.upload_required
@deco.json_response
def fields(request):
    """/<issue>/fields - Gets/Sets fields on the issue.

  Used by upload.py or similar scripts for partial updates of the issue
  without a patchset..
  """
    # Only recognizes a few fields for now.
    if request.method != "POST":
        fields = request.GET.getlist("field")
        response = {}
        if "reviewers" in fields:
            response["reviewers"] = request.issue.reviewers or []
        if "description" in fields:
            response["description"] = request.issue.description
        if "subject" in fields:
            response["subject"] = request.issue.subject
        return response

    if not request.issue.edit_allowed:
        if not IS_DEV:
            return HttpTextResponse("Login required", status=401)
    fields = json.loads(request.POST.get("fields"))
    issue = request.issue
    if "description" in fields:
        issue.description = fields["description"]
    if "reviewers" in fields:
        issue.reviewers = _get_emails_from_raw(fields["reviewers"])
        issue.calculate_updates_for()
    if "subject" in fields:
        issue.subject = fields["subject"]
    issue.put()
    return HttpTextResponse("")


@deco.patch_required
def patch(request):
    """/<issue>/patch/<patchset>/<patch> - View a raw patch."""
    return patch_helper(request)


def patch_helper(request, nav_type="patch"):
    """Returns a unified diff.

  Args:
    request: Django Request object.
    nav_type: the navigation used in the url (i.e. patch/diff/diff2).  Normally
      the user looks at either unified or side-by-side diffs at one time, going
      through all the files in the same mode.  However, if side-by-side is not
      available for some files, we temporarly switch them to unified view, then
      switch them back when we can.  This way they don't miss any files.

  Returns:
    Whatever respond() returns.
  """
    _add_next_prev(request.patchset, request.patch)
    request.patch.nav_type = nav_type
    parsed_lines = patching.ParsePatchToLines(request.patch.lines)
    if parsed_lines is None:
        return HttpTextResponse("Can't parse the patch to lines", status=404)
    rows = engine.RenderUnifiedTableRows(request, parsed_lines)
    return respond(
        request,
        "patch.html",
        {
            "patch": request.patch,
            "patchset": request.patchset,
            "view_style": "patch",
            "rows": rows,
            "issue": request.issue,
            "context": _clean_int(request.GET.get("context"), -1),
            "column_width": _clean_int(request.GET.get("column_width"), None),
        },
    )


@deco.access_control_allow_origin_star
@deco.image_required
def image(request):
    """/<issue>/content/<patchset>/<patch>/<content> - Return patch's content."""
    response = HttpResponse(request.content.data, content_type=request.mime_type)
    filename = re.sub(
        r"[^\w\.]", "_", request.patch.filename.encode("ascii", "replace")
    )
    response["Content-Disposition"] = 'attachment; filename="%s"' % filename
    response["Cache-Control"] = "no-cache, no-store"
    return response


@deco.access_control_allow_origin_star
@deco.patch_required
def download_patch(request):
    """/download/issue<issue>_<patchset>_<patch>.diff - Download patch."""
    return HttpTextResponse(request.patch.text)


def _issue_as_dict(issue, messages, request=None):
    """Converts an issue into a dict."""
    values = {
        "owner": library.get_nickname(issue.owner, True, request),
        "owner_email": issue.owner.email(),
        "modified": str(issue.modified),
        "created": str(issue.created),
        "closed": issue.closed,
        "cc": issue.cc,
        "reviewers": issue.reviewers,
        "patchsets": [p.key.id() for p in issue.patchsets],
        "description": issue.description,
        "subject": issue.subject,
        "project": issue.project,
        "issue": issue.key.id(),
        "base_url": issue.base,
        "private": issue.private,
    }
    if messages:
        values["messages"] = sorted(
            (
                {
                    "sender": m.sender,
                    "recipients": m.recipients,
                    "date": str(m.date),
                    "text": m.text,
                    "approval": m.approval,
                    "disapproval": m.disapproval,
                }
                for m in models.Message.query(ancestor=issue.key)
            ),
            key=lambda x: x["date"],
        )
    return values


def _patchset_as_dict(patchset, comments, request):
    """Converts a patchset into a dict."""
    issue = patchset.issue_key.get()
    values = {
        "patchset": patchset.key.id(),
        "issue": issue.key.id(),
        "owner": library.get_nickname(issue.owner, True, request),
        "owner_email": issue.owner.email(),
        "message": patchset.message,
        "url": patchset.url,
        "created": str(patchset.created),
        "modified": str(patchset.modified),
        "num_comments": patchset.num_comments,
        "files": {},
    }
    for patch in models.Patch.query(models.Patch.patchset_key == patchset.key):
        # num_comments and num_drafts are left out for performance reason:
        # they cause a datastore query on first access. They could be added
        # optionally if the need ever arises.
        values["files"][patch.filename] = {
            "id": patch.key.id(),
            "is_binary": patch.is_binary,
            "no_base_file": patch.no_base_file,
            "num_added": patch.num_added,
            "num_chunks": patch.num_chunks,
            "num_removed": patch.num_removed,
            "status": patch.status,
            "property_changes": "\n".join(patch.property_changes),
        }
        if comments:
            visible_comments = []
            requester_email = request.user.email() if request.user else "no email"
            query = models.Comment.query(models.Comment.patch_key == patch.key).order(
                models.Comment.date
            )
            for c in query:
                if not c.draft or requester_email == c.author.email():
                    visible_comments.append(
                        {
                            "author": library.get_nickname(c.author, True, request),
                            "author_email": c.author.email(),
                            "date": str(c.date),
                            "lineno": c.lineno,
                            "text": c.text,
                            "left": c.left,
                            "draft": c.draft,
                            "message_id": c.message_id,
                        }
                    )

            values["files"][patch.filename]["messages"] = visible_comments

    return values


@deco.access_control_allow_origin_star
@deco.issue_required
@deco.json_response
def api_issue(request):
    """/api/<issue> - Gets issue's data as a JSON-encoded dictionary."""
    messages = request.GET.get("messages", "false").lower() == "true"
    values = _issue_as_dict(request.issue, messages, request)
    return values


@deco.access_control_allow_origin_star
@deco.patchset_required
@deco.json_response
def api_patchset(request):
    """/api/<issue>/<patchset> - Gets an issue's patchset data as a JSON-encoded
  dictionary.
  """
    comments = request.GET.get("comments", "false").lower() == "true"
    values = _patchset_as_dict(request.patchset, comments, request)
    return values


def _get_context_for_user(request):
    """Returns the context setting for a user.

  The value is validated against models.CONTEXT_CHOICES.
  If an invalid value is found, the value is overwritten with
  django_settings.DEFAULT_CONTEXT.
  """
    get_param = request.GET.get("context") or None
    if "context" in request.GET and get_param is None:
        # User wants to see whole file. No further processing is needed.
        return get_param
    if request.user:
        account = models.Account.current_user_account
        default_context = account.default_context
    else:
        default_context = django_settings.DEFAULT_CONTEXT
    context = _clean_int(get_param, default_context)
    if context is not None and context not in models.CONTEXT_CHOICES:
        context = django_settings.DEFAULT_CONTEXT
    return context


def _get_column_width_for_user(request):
    """Returns the column width setting for a user."""
    if request.user:
        account = models.Account.current_user_account
        default_column_width = account.default_column_width
    else:
        default_column_width = django_settings.DEFAULT_COLUMN_WIDTH
    column_width = _clean_int(
        request.GET.get("column_width"),
        default_column_width,
        django_settings.MIN_COLUMN_WIDTH,
        django_settings.MAX_COLUMN_WIDTH,
    )
    return column_width


@deco.patch_filename_required
def diff(request):
    """/<issue>/diff/<patchset>/<patch> - View a patch as a side-by-side diff"""
    if request.patch.no_base_file:
        # Can't show side-by-side diff since we don't have the base file.  Show the
        # unified diff instead.
        return patch_helper(request, "diff")

    patchset = request.patchset
    patch = request.patch

    patchsets = list(request.issue.patchsets)

    context = _get_context_for_user(request)
    column_width = _get_column_width_for_user(request)
    if patch.is_binary:
        rows = None
    else:
        try:
            rows = _get_diff_table_rows(request, patch, context, column_width)
        except FetchError as err:
            return HttpTextResponse(str(err), status=404)

    _add_next_prev(patchset, patch)
    return respond(
        request,
        "diff.html",
        {
            "issue": request.issue,
            "patchset": patchset,
            "patch": patch,
            "view_style": "diff",
            "rows": rows,
            "context": context,
            "context_values": models.CONTEXT_CHOICES,
            "column_width": column_width,
            "patchsets": patchsets,
        },
    )


def _get_diff_table_rows(request, patch, context, column_width):
    """Helper function that returns rendered rows for a patch.

  Raises:
    FetchError if patch parsing or download of base files fails.
  """
    chunks = patching.ParsePatchToChunks(patch.lines, patch.filename)
    if chunks is None:
        raise FetchError("Can't parse the patch to chunks")

    # Possible FetchErrors are handled in diff() and diff_skipped_lines().
    content = request.patch.get_content()

    rows = list(
        engine.RenderDiffTableRows(
            request,
            content.lines,
            chunks,
            patch,
            context=context,
            colwidth=column_width,
        )
    )
    if rows and rows[-1] is None:
        del rows[-1]
        # Get rid of content, which may be bad
        if content.is_uploaded and content.text != None:
            # Don't delete uploaded content, otherwise get_content()
            # will fetch it.
            content.is_bad = True
            content.text = None
            content.put()
        else:
            content.key.delete()
            request.patch.content_key = None
            request.patch.put()

    return rows


@deco.patch_required
@deco.json_response
def diff_skipped_lines(request, id_before, id_after, where, column_width):
    """/<issue>/diff/<patchset>/<patch> - Returns a fragment of skipped lines.

  *where* indicates which lines should be expanded:
    'b' - move marker line to bottom and expand above
    't' - move marker line to top and expand below
    'a' - expand all skipped lines
  """
    patch = request.patch
    if where == "a":
        context = None
    else:
        context = _get_context_for_user(request) or 100

    column_width = _clean_int(
        column_width,
        django_settings.DEFAULT_COLUMN_WIDTH,
        django_settings.MIN_COLUMN_WIDTH,
        django_settings.MAX_COLUMN_WIDTH,
    )

    try:
        rows = _get_diff_table_rows(request, patch, None, column_width)
    except FetchError as err:
        return HttpTextResponse("Error: %s; please report!" % err, status=500)
    return _get_skipped_lines_response(rows, id_before, id_after, where, context)


# there's no easy way to put a control character into a regex, so brute-force it
# this is all control characters except \r, \n, and \t
_badchars_re = re.compile(
    r"[\000\001\002\003\004\005\006\007\010\013\014\016\017"
    r"\020\021\022\023\024\025\026\027\030\031\032\033\034\035\036\037]"
)


def _strip_invalid_xml(s):
    """Remove control chars other than \r\n\t from a string to be put in XML."""
    if _badchars_re.search(s):
        return "".join(c for c in s if c >= " " or c in "\r\n\t")
    else:
        return s


def _get_skipped_lines_response(rows, id_before, id_after, where, context):
    """Helper function that returns response data for skipped lines"""
    response_rows = []
    id_before_start = int(id_before)
    id_after_end = int(id_after)
    if context is not None:
        id_before_end = id_before_start + context
        id_after_start = id_after_end - context
    else:
        id_before_end = id_after_start = None

    for row in rows:
        m = re.match('^<tr( name="hook")? id="pair-(?P<rowcount>\d+)">', row)
        if m:
            curr_id = int(m.groupdict().get("rowcount"))
            # expand below marker line
            if where == "b" and curr_id > id_after_start and curr_id <= id_after_end:
                response_rows.append(row)
            # expand above marker line
            elif (
                where == "t" and curr_id >= id_before_start and curr_id < id_before_end
            ):
                response_rows.append(row)
            # expand all skipped lines
            elif (
                where == "a" and curr_id >= id_before_start and curr_id <= id_after_end
            ):
                response_rows.append(row)
            if context is not None and len(response_rows) >= 2 * context:
                break

    # Create a usable structure for the JS part
    response = []
    response_rows = [_strip_invalid_xml(r) for r in response_rows]
    dom = ElementTree.parse(StringIO("<div>%s</div>" % "".join(response_rows)))
    for node in dom.getroot().getchildren():
        content = [[list(x.items()), x.text] for x in node.getchildren()]
        response.append([list(node.items()), content])
    return response


def _get_diff2_data(
    request,
    ps_left_id,
    ps_right_id,
    patch_id,
    context,
    column_width,
    patch_filename=None,
):
    """Helper function that returns objects for diff2 views"""
    ps_left = models.PatchSet.get_by_id(int(ps_left_id), parent=request.issue.key)
    if ps_left is None:
        return HttpTextResponse(
            "No patch set exists with that id (%s)" % ps_left_id, status=404
        )
    ps_left.issue_key = request.issue.key
    ps_right = models.PatchSet.get_by_id(int(ps_right_id), parent=request.issue.key)
    if ps_right is None:
        return HttpTextResponse(
            "No patch set exists with that id (%s)" % ps_right_id, status=404
        )
    ps_right.issue_key = request.issue.key
    if patch_id is not None:
        patch_right = models.Patch.get_by_id(int(patch_id), parent=ps_right.key)
    else:
        patch_right = None
    if patch_right is not None:
        patch_right.patchset_key = ps_right.key
        if patch_filename is None:
            patch_filename = patch_right.filename
    # Now find the corresponding patch in ps_left
    patch_left = models.Patch.query(
        models.Patch.patchset_key == ps_left.key,
        models.Patch.filename == patch_filename,
    ).get()

    if patch_left:
        try:
            new_content_left = patch_left.get_patched_content()
        except FetchError as err:
            return HttpTextResponse(str(err), status=404)
        lines_left = new_content_left.lines
    elif patch_right:
        lines_left = patch_right.get_content().lines
    else:
        lines_left = []

    if patch_right:
        try:
            new_content_right = patch_right.get_patched_content()
        except FetchError as err:
            return HttpTextResponse(str(err), status=404)
        lines_right = new_content_right.lines
    elif patch_left:
        lines_right = patch_left.get_content().lines
    else:
        lines_right = []

    rows = engine.RenderDiff2TableRows(
        request,
        lines_left,
        patch_left,
        lines_right,
        patch_right,
        context=context,
        colwidth=column_width,
    )
    rows = list(rows)
    if rows and rows[-1] is None:
        del rows[-1]

    return dict(
        patch_left=patch_left,
        patch_right=patch_right,
        ps_left=ps_left,
        ps_right=ps_right,
        rows=rows,
    )


@deco.issue_required
def diff2(request, ps_left_id, ps_right_id, patch_filename):
    """/<issue>/diff2/... - View the delta between two different patch sets."""
    context = _get_context_for_user(request)
    column_width = _get_column_width_for_user(request)

    ps_right = models.PatchSet.get_by_id(int(ps_right_id), parent=request.issue.key)
    patch_right = None

    if ps_right:
        patch_right = models.Patch.query(
            models.Patch.patchset_key == ps_right.key,
            models.Patch.filename == patch_filename,
        ).get()

    if patch_right:
        patch_id = patch_right.key.id()
    elif patch_filename.isdigit():
        # Perhaps it's an ID that's passed in, based on the old URL scheme.
        patch_id = int(patch_filename)
    else:  # patch doesn't exist in this patchset
        patch_id = None

    data = _get_diff2_data(
        request,
        ps_left_id,
        ps_right_id,
        patch_id,
        context,
        column_width,
        patch_filename,
    )
    if isinstance(data, HttpResponse) and data.status_code != 302:
        return data

    patchsets = list(request.issue.patchsets)

    if data["patch_right"]:
        _add_next_prev2(data["ps_left"], data["ps_right"], data["patch_right"])
    return respond(
        request,
        "diff2.html",
        {
            "issue": request.issue,
            "ps_left": data["ps_left"],
            "patch_left": data["patch_left"],
            "ps_right": data["ps_right"],
            "patch_right": data["patch_right"],
            "rows": data["rows"],
            "patch_id": patch_id,
            "context": context,
            "context_values": models.CONTEXT_CHOICES,
            "column_width": column_width,
            "patchsets": patchsets,
            "filename": patch_filename,
        },
    )


@deco.issue_required
@deco.json_response
def diff2_skipped_lines(
    request, ps_left_id, ps_right_id, patch_id, id_before, id_after, where, column_width
):
    """/<issue>/diff2/... - Returns a fragment of skipped lines"""
    column_width = _clean_int(
        column_width,
        django_settings.DEFAULT_COLUMN_WIDTH,
        django_settings.MIN_COLUMN_WIDTH,
        django_settings.MAX_COLUMN_WIDTH,
    )

    if where == "a":
        context = None
    else:
        context = _get_context_for_user(request) or 100

    data = _get_diff2_data(
        request, ps_left_id, ps_right_id, patch_id, 10000, column_width
    )
    if isinstance(data, HttpResponse) and data.status_code != 302:
        return data
    return _get_skipped_lines_response(
        data["rows"], id_before, id_after, where, context
    )


def _get_comment_counts(account, patchset):
    """Helper to get comment counts for all patches in a single query.

  The helper returns two dictionaries comments_by_patch and
  drafts_by_patch with patch key as key and comment count as
  value. Patches without comments or drafts are not present in those
  dictionaries.
  """
    # A key-only query won't work because we need to fetch the patch key
    # in the for loop further down.
    comment_query = models.Comment.query(ancestor=patchset.key)

    # Get all comment counts with one query rather than one per patch.
    comments_by_patch = {}
    drafts_by_patch = {}
    for c in comment_query:
        pkey = c.patch_key
        if not c.draft:
            comments_by_patch[pkey] = comments_by_patch.setdefault(pkey, 0) + 1
        elif account and c.author == account.user:
            drafts_by_patch[pkey] = drafts_by_patch.setdefault(pkey, 0) + 1

    return comments_by_patch, drafts_by_patch


def _add_next_prev(patchset, patch):
    """Helper to add .next and .prev attributes to a patch object."""
    patch.prev = patch.__next__ = None
    patches = list(patchset.patches)
    patchset.patches_cache = patches  # Required to render the jump to select.

    comments_by_patch, drafts_by_patch = _get_comment_counts(
        models.Account.current_user_account, patchset
    )

    last_patch = None
    next_patch = None
    last_patch_with_comment = None
    next_patch_with_comment = None

    found_patch = False
    for p in patches:
        if p.filename == patch.filename:
            found_patch = True
            continue

        p._num_comments = comments_by_patch.get(p.key, 0)
        p._num_drafts = drafts_by_patch.get(p.key, 0)

        if not found_patch:
            last_patch = p
            if p.num_comments > 0 or p.num_drafts > 0:
                last_patch_with_comment = p
        else:
            if next_patch is None:
                next_patch = p
            if p.num_comments > 0 or p.num_drafts > 0:
                next_patch_with_comment = p
                # safe to stop scanning now because the next with out a comment
                # will already have been filled in by some earlier patch
                break

    patch.prev = last_patch
    patch.next = next_patch
    patch.prev_with_comment = last_patch_with_comment
    patch.next_with_comment = next_patch_with_comment


def _add_next_prev2(ps_left, ps_right, patch_right):
    """Helper to add .next and .prev attributes to a patch object."""
    patch_right.prev = patch_right.__next__ = None
    patches = list(ps_right.patches)
    ps_right.patches_cache = patches  # Required to render the jump to select.

    n_comments, n_drafts = _get_comment_counts(
        models.Account.current_user_account, ps_right
    )

    last_patch = None
    next_patch = None
    last_patch_with_comment = None
    next_patch_with_comment = None

    found_patch = False
    for p in patches:
        if p.filename == patch_right.filename:
            found_patch = True
            continue

        p._num_comments = n_comments.get(p.key, 0)
        p._num_drafts = n_drafts.get(p.key, 0)

        if not found_patch:
            last_patch = p
            if (p.num_comments > 0 or p.num_drafts > 0) and ps_left.key.id() in p.delta:
                last_patch_with_comment = p
        else:
            if next_patch is None:
                next_patch = p
            if (p.num_comments > 0 or p.num_drafts > 0) and ps_left.key.id() in p.delta:
                next_patch_with_comment = p
                # safe to stop scanning now because the next with out a comment
                # will already have been filled in by some earlier patch
                break

    patch_right.prev = last_patch
    patch_right.next = next_patch
    patch_right.prev_with_comment = last_patch_with_comment
    patch_right.next_with_comment = next_patch_with_comment


def _add_or_update_comment(user, issue, patch, lineno, left, text, message_id):
    comment = None
    if message_id:
        comment = models.Comment.get_by_id(message_id, parent=patch.key)
        if comment is None or not comment.draft or comment.author != user:
            comment = None
            message_id = None
    if not message_id:
        # Prefix with 'z' to avoid key names starting with digits.
        message_id = "z" + binascii.hexlify(_random_bytes(16))

    if not text.rstrip():
        if comment is not None:
            assert comment.draft and comment.author == user
            comment.key.delete()  # Deletion
            comment = None
            # Re-query the comment count.
            models.Account.current_user_account.update_drafts(issue)
    else:
        if comment is None:
            comment = models.Comment(id=message_id, parent=patch.key)
        comment.patch_key = patch.key
        comment.lineno = lineno
        comment.left = left
        comment.text = text
        comment.message_id = message_id
        comment.put()
        # The actual count doesn't matter, just that there's at least one.
        models.Account.current_user_account.update_drafts(issue, 1)
    return comment


@deco.login_required
@deco.patchset_required
@deco.require_methods("POST")
@deco.json_response
def api_draft_comments(request):
    """/api/<issue>/<patchset>/draft_comments - Store a number of draft
  comments for a particular issue and patchset.

  This API differs from inline_draft in two ways:

  1) api_draft_comments handles multiple comments at once so that
     clients can upload draft comments in bulk.
  2) api_draft_comments returns a response in JSON rather than
     in HTML, which lets clients process the response programmatically.

  Note: creating or editing draft comments is *not* XSRF-protected,
  because it is not unusual to come back after hours; the XSRF tokens
  time out after 1 or 2 hours.  The final submit of the drafts for
  others to view *is* XSRF-protected.
  """
    try:

        def sanitize(comment):
            patch = models.Patch.get_by_id(
                int(comment.patch_id), parent=request.patchset.key
            )
            assert not patch is None
            message_id = (str(comment.message_id) if message_id in comment else None,)
            return {
                user: request.user,
                issue: request.issue,
                patch: patch,
                lineno: int(comment.lineno),
                left: bool(comment.left),
                text: str(comment.text),
                message_id: message_id,
            }

        return [
            {message_id: _add_or_update_comment(**comment).message_id}
            for comment in map(sanitize, json.load(request.data))
        ]
    except Exception as err:
        return HttpTextResponse("An error occurred.", status=500)


@deco.require_methods("POST")
def inline_draft(request):
    """/inline_draft - Ajax handler to submit an in-line draft comment.

  This wraps _inline_draft(); all exceptions are logged and cause an
  abbreviated response indicating something went wrong.

  Note: creating or editing draft comments is *not* XSRF-protected,
  because it is not unusual to come back after hours; the XSRF tokens
  time out after 1 or 2 hours.  The final submit of the drafts for
  others to view *is* XSRF-protected.
  """
    try:
        return _inline_draft(request)
    except Exception as err:
        logging.exception("Exception in inline_draft processing:")
        # TODO(guido): return some kind of error instead?
        # Return HttpResponse for now because the JS part expects
        # a 200 status code.
        return HttpHtmlResponse(
            '<font color="red">Error: %s; please report!</font>'
            % err.__class__.__name__
        )


def _inline_draft(request):
    """Helper to submit an in-line draft comment."""
    # TODO(guido): turn asserts marked with XXX into errors
    # Don't use @login_required, since the JS doesn't understand redirects.
    if not request.user:
        # Don't log this, spammers have started abusing this.
        return HttpTextResponse("Not logged in")
    snapshot = request.POST.get("snapshot")
    assert snapshot in ("old", "new"), repr(snapshot)
    left = snapshot == "old"
    side = request.POST.get("side")
    assert side in ("a", "b"), repr(side)  # Display left (a) or right (b)
    issue_id = int(request.POST["issue"])
    issue = models.Issue.get_by_id(issue_id)
    assert issue  # XXX
    patchset_id = int(
        request.POST.get("patchset")
        or request.POST[side == "a" and "ps_left" or "ps_right"]
    )
    patchset = models.PatchSet.get_by_id(int(patchset_id), parent=issue.key)
    assert patchset  # XXX
    patch_id = int(
        request.POST.get("patch")
        or request.POST[side == "a" and "patch_left" or "patch_right"]
    )
    patch = models.Patch.get_by_id(int(patch_id), parent=patchset.key)
    assert patch  # XXX
    text = request.POST.get("text")
    lineno = int(request.POST["lineno"])
    message_id = request.POST.get("message_id")
    comment = _add_or_update_comment(
        user=request.user,
        issue=issue,
        patch=patch,
        lineno=lineno,
        left=left,
        text=text,
        message_id=message_id,
    )
    issue.calculate_draft_count_by_user()
    issue_fut = issue.put_async()

    query = models.Comment.query(
        models.Comment.patch_key == patch.key,
        models.Comment.lineno == lineno,
        models.Comment.left == left,
    ).order(models.Comment.date)
    comments = list(c for c in query if not c.draft or c.author == request.user)
    if comment is not None and comment.author is None:
        # Show anonymous draft even though we don't save it
        comments.append(comment)
    issue_fut.get_result()
    if not comments:
        return HttpTextResponse(" ")
    for c in comments:
        c.complete()
    return render_to_response(
        "inline_comment.html",
        {
            "user": request.user,
            "patch": patch,
            "patchset": patchset,
            "issue": issue,
            "comments": comments,
            "lineno": lineno,
            "snapshot": snapshot,
            "side": side,
        },
        context_instance=RequestContext(request),
    )


def _get_affected_files(issue, full_diff=False):
    """Helper to return a list of affected files from the latest patchset.

  Args:
    issue: Issue instance.
    full_diff: If true, include the entire diff even if it exceeds 100 lines.

  Returns:
    2-tuple containing a list of affected files, and the diff contents if it
    is less than 100 lines (otherwise the second item is an empty string).
  """
    files = []
    modified_count = 0
    diff = ""
    patchsets = list(issue.patchsets)
    if len(patchsets):
        patchset = patchsets[-1]
        for patch in patchset.patches:
            file_str = ""
            if patch.status:
                file_str += patch.status + " "
            file_str += patch.filename
            files.append(file_str)
            # No point in loading patches if the patchset is too large for email.
            if full_diff or modified_count < 100:
                modified_count += patch.num_added + patch.num_removed

        if full_diff or modified_count < 100:
            diff = patchset.data

    return files, diff


def _get_mail_template(request, issue, full_diff=False):
    """Helper to return the template and context for an email.

  If this is the first email sent by the owner, a template that lists the
  reviewers, description and files is used.
  """
    context = {}
    template = "mails/comment.txt"
    if request.user == issue.owner:
        query = models.Message.query(
            models.Message.sender == request.user.email(), ancestor=issue.key
        )
        if query.count(1) == 0:
            template = "mails/review.txt"
            files, patch = _get_affected_files(issue, full_diff)
            context.update({"files": files, "patch": patch, "base": issue.base})
    return template, context


@deco.login_required
@deco.issue_required
@deco.xsrf_required
def publish(request):
    """ /<issue>/publish - Publish draft comments and send mail."""
    issue = request.issue
    if issue.edit_allowed:
        form_class = PublishForm
    else:
        form_class = MiniPublishForm
    draft_message = None
    if not request.POST.get("message_only", None):
        query = models.Message.query(
            models.Message.issue_key == issue.key,
            models.Message.sender == request.user.email(),
            models.Message.draft == True,
        )
        draft_message = query.get()
    if request.method != "POST":
        reviewers = issue.reviewers[:]
        cc = issue.cc[:]
        if (
            request.user != issue.owner
            and request.user.email() not in issue.reviewers
            and not issue.is_collaborator(request.user)
        ):
            reviewers.append(request.user.email())
            if request.user.email() in cc:
                cc.remove(request.user.email())
        reviewers = [
            models.Account.get_nickname_for_email(reviewer, default=reviewer)
            for reviewer in reviewers
        ]
        ccs = [models.Account.get_nickname_for_email(cc, default=cc) for cc in cc]
        tbd, comments = _get_draft_comments(request, issue, True)
        preview = _get_draft_details(request, comments)
        if draft_message is None:
            msg = ""
        else:
            msg = draft_message.text
        form = form_class(
            initial={
                "subject": issue.subject,
                "reviewers": ", ".join(reviewers),
                "cc": ", ".join(ccs),
                "send_mail": True,
                "message": msg,
            }
        )
        return respond(
            request,
            "publish.html",
            {
                "form": form,
                "issue": issue,
                "preview": preview,
                "draft_message": draft_message,
            },
        )

    # Supply subject so that if this is a bare request to /publish, it won't
    # fail out if we've selected PublishForm (which requires a subject).
    augmented_POST = request.POST.copy()
    if issue.subject:
        augmented_POST.setdefault("subject", issue.subject)
    form = form_class(augmented_POST)

    # If the user is blocked, intentionally redirects him to the form again to
    # confuse him.
    account = models.Account.get_account_for_user(request.user)
    if account.blocked or not form.is_valid():
        return respond(request, "publish.html", {"form": form, "issue": issue})
    if issue.edit_allowed:
        issue.subject = form.cleaned_data["subject"]
    if form.is_valid() and not form.cleaned_data.get("message_only", False):
        reviewers = _get_emails(form, "reviewers")
    else:
        reviewers = issue.reviewers
        if (
            request.user != issue.owner
            and request.user.email() not in reviewers
            and not issue.is_collaborator(request.user)
        ):
            reviewers.append(db.Email(request.user.email()))
    if form.is_valid() and not form.cleaned_data.get("message_only", False):
        cc = _get_emails(form, "cc")
    else:
        cc = issue.cc
        # The user is in the reviewer list, remove them from CC if they're there.
        if request.user.email() in cc:
            cc.remove(request.user.email())
    if not form.is_valid():
        return respond(request, "publish.html", {"form": form, "issue": issue})
    issue.reviewers = reviewers
    issue.cc = cc
    if not form.cleaned_data.get("message_only", False):
        tbd, comments = _get_draft_comments(request, issue)
    else:
        tbd = []
        comments = []
    issue.update_comment_count(len(comments))
    tbd.append(issue)

    if comments:
        logging.warn("Publishing %d comments", len(comments))
    msg = _make_message(
        request,
        issue,
        form.cleaned_data["message"],
        comments,
        form.cleaned_data["send_mail"],
        draft=draft_message,
        in_reply_to=form.cleaned_data.get("in_reply_to"),
    )
    tbd.append(msg)

    for obj in tbd:
        obj.put()

    # There are now no comments here (modulo race conditions)
    models.Account.current_user_account.update_drafts(issue, 0)
    if form.cleaned_data.get("no_redirect", False):
        return HttpTextResponse("OK")
    return HttpResponseRedirect(reverse(show, args=[issue.key.id()]))


@deco.login_required
@deco.issue_required
@deco.xsrf_required
def delete_drafts(request):
    """Deletes all drafts of the current user for an issue."""
    query = models.Comment.query(
        models.Comment.author == request.user,
        models.Comment.draft == True,
        ancestor=request.issue.key,
    )
    keys = query.fetch(keys_only=True)
    ndb.delete_multi(keys)
    request.issue.calculate_draft_count_by_user()
    request.issue.put()
    return HttpResponseRedirect(reverse(publish, args=[request.issue.key.id()]))


def _encode_safely(s):
    """Helper to turn a unicode string into 8-bit bytes."""
    if isinstance(s, str):
        s = s.encode("utf-8")
    return s


def _get_draft_comments(request, issue, preview=False):
    """Helper to return objects to put() and a list of draft comments.

  If preview is True, the list of objects to put() is empty to avoid changes
  to the datastore.

  Args:
    request: Django Request object.
    issue: Issue instance.
    preview: Preview flag (default: False).

  Returns:
    2-tuple (put_objects, comments).
  """
    comments = []
    tbd = []
    # XXX Should request all drafts for this issue once, now we can.
    for patchset in issue.patchsets:
        ps_comments = list(
            models.Comment.query(
                models.Comment.author == request.user,
                models.Comment.draft == True,
                ancestor=patchset.key,
            )
        )
        if ps_comments:
            patches = dict((p.key, p) for p in patchset.patches)
            for p in patches.values():
                p.patchset_key = patchset.key
            for c in ps_comments:
                c.draft = False
                # Get the patch key value without loading the patch entity.
                # NOTE: Unlike the old version of this code, this is the
                # recommended and documented way to do this!
                pkey = c.patch_key
                if pkey in patches:
                    patch = patches[pkey]
                    c.patch_key = patch.key
            if not preview:
                tbd.extend(ps_comments)
                patchset.update_comment_count(len(ps_comments))
                tbd.append(patchset)
            ps_comments.sort(
                key=lambda c: (c.patch_key.get().filename, not c.left, c.lineno, c.date)
            )
            comments += ps_comments

    return tbd, comments


def _patchlines2cache(patchlines, left):
    """Helper that converts return value of ParsePatchToLines for caching.

  Each line in patchlines is (old_line_no, new_line_no, line).  When
  comment is on the left we store the old_line_no, otherwise
  new_line_no.
  """
    if left:
        it = ((old, line) for old, _, line in patchlines)
    else:
        it = ((new, line) for _, new, line in patchlines)
    return dict(it)


def _get_draft_details(request, comments):
    """Helper to display comments with context in the email message."""
    last_key = None
    output = []
    linecache = {}  # Maps (c.patch_key, c.left) to mapping (lineno, line)
    modified_patches = []
    fetch_base_failed = False

    for c in comments:
        patch = c.patch_key.get()
        if (patch.key, c.left) != last_key:
            url = request.build_absolute_uri(
                reverse(
                    diff,
                    args=[
                        request.issue.key.id(),
                        patch.patchset_key.id(),
                        patch.filename,
                    ],
                )
            )
            output.append(
                "\n%s\nFile %s (%s):"
                % (url, patch.filename, c.left and "left" or "right")
            )
            last_key = (patch.key, c.left)
            if patch.no_base_file:
                linecache[last_key] = _patchlines2cache(
                    patching.ParsePatchToLines(patch.lines), c.left
                )
            else:
                try:
                    if c.left:
                        old_lines = patch.get_content().text.splitlines(True)
                        linecache[last_key] = dict(enumerate(old_lines, 1))
                    else:
                        new_lines = patch.get_patched_content().text.splitlines(True)
                        linecache[last_key] = dict(enumerate(new_lines, 1))
                except FetchError:
                    linecache[last_key] = _patchlines2cache(
                        patching.ParsePatchToLines(patch.lines), c.left
                    )
                    fetch_base_failed = True
        context = linecache[last_key].get(c.lineno, "").strip()
        url = request.build_absolute_uri(
            "%s#%scode%d"
            % (
                reverse(
                    diff,
                    args=[
                        request.issue.key.id(),
                        patch.patchset_key.id(),
                        patch.filename,
                    ],
                ),
                c.left and "old" or "new",
                c.lineno,
            )
        )
        output.append(
            "\n%s\n%s:%d: %s\n%s"
            % (url, patch.filename, c.lineno, context, c.text.rstrip())
        )
    if modified_patches:
        ndb.put_multi(modified_patches)
    return "\n".join(output)


def _get_modified_counts(issue):
    """Helper to determine the modified line counts of the latest patch set."""
    modified_added_count = 0
    modified_removed_count = 0

    # Count the modified lines in the patchset.
    patchsets = list(issue.patchsets)
    if patchsets:
        for patch in patchsets[-1].patches:
            modified_added_count += patch.num_added
            modified_removed_count += patch.num_removed

    return modified_added_count, modified_removed_count


def _make_message(
    request,
    issue,
    message,
    comments=None,
    send_mail=False,
    draft=None,
    in_reply_to=None,
):
    """Helper to create a Message instance and optionally send an email."""
    attach_patch = request.POST.get("attach_patch") == "yes"
    template, context = _get_mail_template(request, issue, full_diff=attach_patch)
    # Decide who should receive mail
    my_email = db.Email(request.user.email())
    to = (
        [db.Email(issue.owner.email())]
        + issue.reviewers
        + [db.Email(email) for email in issue.collaborator_emails()]
    )
    cc = issue.cc[:]
    if django_settings.RIETVELD_INCOMING_MAIL_ADDRESS:
        cc.append(db.Email(django_settings.RIETVELD_INCOMING_MAIL_ADDRESS))
    reply_to = to + cc
    if my_email in to and len(to) > 1:  # send_mail() wants a non-empty to list
        to.remove(my_email)
    if my_email in cc:
        cc.remove(my_email)
    issue_id = issue.key.id()
    subject = issue.mail_subject()
    patch = None
    if attach_patch:
        subject = "PATCH: " + subject
        if "patch" in context:
            patch = context["patch"]
            del context["patch"]
    if issue.num_messages:
        subject = "Re: " + subject
    if comments:
        details = _get_draft_details(request, comments)
    else:
        details = ""
    message = message.replace("\r\n", "\n")
    text = ((message.strip() + "\n\n" + details.strip())).strip()
    if draft is None:
        msg = models.Message(
            issue_key=issue.key,
            subject=subject,
            sender=my_email,
            recipients=reply_to,
            text=text,
            parent=issue.key,
            issue_was_closed=issue.closed,
        )
    else:
        msg = draft
        msg.subject = subject
        msg.recipients = reply_to
        msg.text = text
        msg.draft = False
        msg.date = datetime.datetime.now()
        msg.issue_was_closed = issue.closed
    issue.calculate_updates_for(msg)

    if in_reply_to:
        try:
            replied_msg_id = int(in_reply_to)
            replied_msg = models.Message.get_by_id(replied_msg_id, parent=issue.key)
            msg.in_reply_to_key = replied_msg.key
            replied_issue_id = replied_msg.issue_key.id()
            if replied_issue_id != issue_id:
                logging.warn(
                    "In-reply-to Message is for a different issue: " "%s instead of %s",
                    replied_issue_id,
                    issue_id,
                )
                msg.in_reply_to_key = None
        except (db.KindError, db.BadKeyError, ValueError):
            logging.warn("Invalid in-reply-to Message or key given: %s", in_reply_to)

    if send_mail:
        # Limit the list of files in the email to approximately 200
        if "files" in context and len(context["files"]) > 210:
            num_trimmed = len(context["files"]) - 200
            del context["files"][200:]
            context["files"].append("[[ %d additional files ]]" % num_trimmed)
        url = request.build_absolute_uri(reverse(show, args=[issue.key.id()]))
        reviewer_nicknames = ", ".join(
            library.get_nickname(rev_temp, True, request)
            for rev_temp in issue.reviewers
        )
        cc_nicknames = ", ".join(
            library.get_nickname(cc_temp, True, request) for cc_temp in cc
        )
        my_nickname = library.get_nickname(request.user, True, request)
        reply_to = ", ".join(reply_to)
        description = (issue.description or "").replace("\r\n", "\n")
        home = request.build_absolute_uri(reverse(index))
        modified_added_count, modified_removed_count = _get_modified_counts(issue)
        context.update(
            {
                "reviewer_nicknames": reviewer_nicknames,
                "cc_nicknames": cc_nicknames,
                "my_nickname": my_nickname,
                "url": url,
                "message": message,
                "details": details,
                "description": description,
                "home": home,
                "added_lines": modified_added_count,
                "removed_lines": modified_removed_count,
            }
        )
        for key, value in context.items():
            if isinstance(value, str):
                try:
                    encoding.force_unicode(value)
                except UnicodeDecodeError:
                    logging.error(
                        "Key %s is not valid unicode. value: %r" % (key, value)
                    )
                    # The content failed to be decoded as utf-8. Enforce it as ASCII.
                    context[key] = value.decode("ascii", "replace")
        body = django.template.loader.render_to_string(
            template, context, context_instance=RequestContext(request)
        )
        logging.warn("Mail: to=%s; cc=%s", ", ".join(to), ", ".join(cc))
        send_args = {
            "sender": my_email,
            "to": [_encode_safely(address) for address in to],
            "subject": _encode_safely(subject),
            "body": _encode_safely(body),
            "reply_to": _encode_safely(reply_to),
        }
        if cc:
            send_args["cc"] = [_encode_safely(address) for address in cc]
        if patch:
            send_args["attachments"] = [("issue_%s_patch.diff" % issue.key.id(), patch)]

        attempts = 0
        while True:
            try:
                mail.send_mail(**send_args)
                break
            except mail.InvalidSenderError:
                if django_settings.RIETVELD_INCOMING_MAIL_ADDRESS:
                    previous_sender = send_args["sender"]
                    if previous_sender not in send_args["to"]:
                        send_args["to"].append(previous_sender)
                    send_args["sender"] = django_settings.RIETVELD_INCOMING_MAIL_ADDRESS
                else:
                    raise
            except apiproxy_errors.DeadlineExceededError:
                # apiproxy_errors.DeadlineExceededError is raised when the
                # deadline of an API call is reached (e.g. for mail it's
                # something about 5 seconds). It's not the same as the lethal
                # runtime.DeadlineExeededError.
                attempts += 1
                if attempts >= 3:
                    raise
        if attempts:
            logging.warning("Retried sending email %s times", attempts)

    return msg


@deco.require_methods("POST")
@deco.login_required
@deco.xsrf_required
@deco.issue_required
def star(request):
    """Add a star to an Issue."""
    account = models.Account.current_user_account
    account.user_has_selected_nickname()  # This will preserve account.fresh.
    if account.stars is None:
        account.stars = []
    keyid = request.issue.key.id()
    if keyid not in account.stars:
        account.stars.append(keyid)
        account.put()
    return respond(request, "issue_star.html", {"issue": request.issue})


@deco.require_methods("POST")
@deco.login_required
@deco.issue_required
@deco.xsrf_required
def unstar(request):
    """Remove the star from an Issue."""
    account = models.Account.current_user_account
    account.user_has_selected_nickname()  # This will preserve account.fresh.
    if account.stars is None:
        account.stars = []
    keyid = request.issue.key.id()
    if keyid in account.stars:
        account.stars[:] = [i for i in account.stars if i != keyid]
        account.put()
    return respond(request, "issue_star.html", {"issue": request.issue})


@deco.login_required
@deco.issue_required
def draft_message(request):
    """/<issue>/draft_message - Retrieve, modify and delete draft messages.

  Note: creating or editing draft messages is *not* XSRF-protected,
  because it is not unusual to come back after hours; the XSRF tokens
  time out after 1 or 2 hours.  The final submit of the drafts for
  others to view *is* XSRF-protected.
  """
    query = models.Message.query(
        models.Message.issue_key == request.issue.key,
        models.Message.sender == request.user.email(),
        models.Message.draft == True,
    )
    if query.count() == 0:
        draft_message = None
    else:
        draft_message = query.get()
    if request.method == "GET":
        return _get_draft_message(draft_message)
    elif request.method == "POST":
        return _post_draft_message(request, draft_message)
    elif request.method == "DELETE":
        return _delete_draft_message(draft_message)
    return HttpTextResponse("An error occurred.", status=500)


def _get_draft_message(draft):
    """Handles GET requests to /<issue>/draft_message.

  Arguments:
    draft: A Message instance or None.

  Returns the content of a draft message or an empty string if draft is None.
  """
    return HttpTextResponse(draft.text if draft else "")


def _post_draft_message(request, draft):
    """Handles POST requests to /<issue>/draft_message.

  If draft is None a new message is created.

  Arguments:
    request: The current request.
    draft: A Message instance or None.
  """
    if draft is None:
        draft = models.Message(
            issue_key=request.issue.key,
            parent=request.issue.key,
            sender=request.user.email(),
            draft=True,
        )
    draft.text = request.POST.get("reviewmsg")
    draft.put()
    return HttpTextResponse(draft.text)


def _delete_draft_message(draft):
    """Handles DELETE requests to /<issue>/draft_message.

  Deletes a draft message.

  Arguments:
    draft: A Message instance or None.
  """
    if draft is not None:
        draft.key.delete()
    return HttpTextResponse("OK")


@deco.json_response
def search(request):
    """/search - Search for issues or patchset.

  Returns HTTP 500 if the corresponding index is missing.
  """
    if request.method == "GET":
        form = SearchForm(request.GET)
        if not form.is_valid() or not request.GET:
            return respond(request, "search.html", {"form": form})
    else:
        form = SearchForm(request.POST)
        if not form.is_valid():
            return HttpTextResponse("Invalid arguments", status=400)
    logging.info("%s" % form.cleaned_data)
    keys_only = form.cleaned_data["keys_only"] or False
    requested_format = form.cleaned_data["format"] or "html"
    limit = form.cleaned_data["limit"]
    with_messages = form.cleaned_data["with_messages"]
    if requested_format == "html":
        keys_only = False
        limit = limit or DEFAULT_LIMIT
    else:
        if not limit:
            if keys_only:
                # It's a fast query.
                limit = 1000
            elif with_messages:
                # It's an heavy query.
                limit = 10
            else:
                limit = 100

    q = models.Issue.query(default_options=ndb.QueryOptions(keys_only=keys_only))
    encoded_cursor = form.cleaned_data["cursor"] or None
    if encoded_cursor:
        cursor = datastore_query.Cursor(urlsafe=encoded_cursor)
    else:
        cursor = None

    if form.cleaned_data["closed"] is not None:
        q = q.filter(models.Issue.closed == form.cleaned_data["closed"])
    if form.cleaned_data["owner"]:
        q = q.filter(models.Issue.owner == form.cleaned_data["owner"])
    if form.cleaned_data["reviewer"]:
        q = q.filter(models.Issue.reviewers == form.cleaned_data["reviewer"])
    if form.cleaned_data["cc"]:
        q = q.filter(models.Issue.cc == form.cleaned_data["cc"])
    if form.cleaned_data["private"] is not None:
        q = q.filter(models.Issue.private == form.cleaned_data["private"])
    if form.cleaned_data["repo_guid"]:
        q = q.filter(models.Issue.repo_guid == form.cleaned_data["repo_guid"])
    if form.cleaned_data["base"]:
        q = q.filter(models.Issue.base == form.cleaned_data["base"])

    # Calculate a default value depending on the query parameter.
    # Prefer sorting by modified date over created date and showing
    # newest first over oldest.
    default_sort = "-modified"
    if form.cleaned_data["created_after"]:
        q = q.filter(models.Issue.created >= form.cleaned_data["created_after"])
        default_sort = "created"
    if form.cleaned_data["modified_after"]:
        q = q.filter(models.Issue.modified >= form.cleaned_data["modified_after"])
        default_sort = "modified"
    if form.cleaned_data["created_before"]:
        q = q.filter(models.Issue.created < form.cleaned_data["created_before"])
        default_sort = "-created"
    if form.cleaned_data["modified_before"]:
        q = q.filter(models.Issue.modified < form.cleaned_data["modified_before"])
        default_sort = "-modified"

    sorted_by = form.cleaned_data["order"] or default_sort
    direction = (
        datastore_query.PropertyOrder.DESCENDING
        if sorted_by.startswith("-")
        else datastore_query.PropertyOrder.ASCENDING
    )
    q = q.order(datastore_query.PropertyOrder(sorted_by.lstrip("-"), direction))

    # Update the cursor value in the result.
    if requested_format == "html":
        nav_params = dict((k, v) for k, v in form.cleaned_data.items() if v is not None)
        return _paginate_issues_with_cursor(
            reverse(search),
            request,
            q,
            cursor,
            limit,
            "search_results.html",
            extra_nav_parameters=nav_params,
        )

    # We do not simply use fetch_page() because we do some post-filtering which
    # could lead to under-filled pages.   Instead, we iterate, filter and keep
    # going until we have enough post-filtered results, then return those along
    # with the cursor after the last item.
    filtered_results = []
    next_cursor = None
    query_iter = q.iter(start_cursor=cursor, produce_cursors=True)

    for result in query_iter:
        if keys_only:
            # There's not enough information to filter. The only thing that is leaked
            # is the issue's key.
            filtered_results.append(result)
        elif result.view_allowed:
            filtered_results.append(result)

        if len(filtered_results) >= limit:
            break

    # If any results are returned, also include a cursor to try to get more.
    if filtered_results:
        next_cursor = query_iter.cursor_after()

    data = {
        "cursor": next_cursor.urlsafe() if next_cursor else "",
    }
    if keys_only:
        data["results"] = [i.id() for i in filtered_results]
    else:
        data["results"] = [
            _issue_as_dict(i, with_messages, request) for i in filtered_results
        ]
    return data


### Repositories and Branches ###


def repos(request):
    """/repos - Show the list of known Subversion repositories."""
    # Clean up garbage created by buggy edits
    bad_branch_keys = models.Branch.query(models.Branch.owner == None).fetch(
        100, keys_only=True
    )
    if bad_branch_keys:
        ndb.delete_multi(bad_branch_keys)
    repo_map = {}
    for repo in models.Repository.query().fetch(1000, batch_size=100):
        repo_map[repo.key] = repo
    branches = []
    for branch in models.Branch.query().fetch(2000, batch_size=100):
        repo_key = branch.repo_key
        if repo_key in repo_map:
            branch.repository = repo_map[repo_key]
            branches.append(branch)
    branches.sort(
        key=lambda b: list(map(str.lower, (b.repository.name, b.category, b.name)))
    )
    return respond(request, "repos.html", {"branches": branches})


@deco.login_required
@deco.xsrf_required
def repo_new(request):
    """/repo_new - Create a new Subversion repository record."""
    if request.method != "POST":
        form = RepoForm()
        return respond(request, "repo_new.html", {"form": form})
    form = RepoForm(request.POST)
    errors = form.errors
    if not errors:
        try:
            repo = models.Repository(
                name=form.cleaned_data.get("name"),
                url=form.cleaned_data.get("url"),
                guid=form.cleaned_data.get("guid"),
            )
        except (db.BadValueError, ValueError) as err:
            errors["__all__"] = str(err)
    if errors:
        return respond(request, "repo_new.html", {"form": form})
    repo.put()
    branch_url = repo.url
    if not branch_url.endswith("/"):
        branch_url += "/"
    branch_url += "trunk/"
    branch = models.Branch(
        repo_key=repo.key,
        repo_name=repo.name,
        category="*trunk*",
        name="Trunk",
        url=branch_url,
    )
    branch.put()
    return HttpResponseRedirect(reverse(repos))


SVN_ROOT = "http://svn.python.org/view/*checkout*/python/"
BRANCHES = [
    # category, name, url suffix
    ("*trunk*", "Trunk", "trunk/"),
    ("branch", "2.5", "branches/release25-maint/"),
    ("branch", "py3k", "branches/py3k/"),
]


# TODO: Make this a POST request to avoid XSRF attacks.
@deco.admin_required
def repo_init(_request):
    """/repo_init - Initialze the list of known Subversion repositories."""
    python = models.Repository.query(models.Repository.name == "Python").get()
    if python is None:
        python = models.Repository(name="Python", url=SVN_ROOT)
        python.put()
        pybranches = []
    else:
        pybranches = list(models.Branch.query(models.Branch.repo_key == python.key))
    for category, name, url in BRANCHES:
        url = python.url + url
        for br in pybranches:
            if (br.category, br.name, br.url) == (category, name, url):
                break
        else:
            br = models.Branch(
                repo_key=python.key,
                repo_name="Python",
                category=category,
                name=name,
                url=url,
            )
            br.put()
    return HttpResponseRedirect(reverse(repos))


@deco.login_required
@deco.xsrf_required
def branch_new(request, repo_id):
    """/branch_new/<repo> - Add a new Branch to a Repository record."""
    repo = models.Repository.get_by_id(int(repo_id))
    if request.method != "POST":
        form = BranchForm(initial={"url": repo.url, "category": "branch",})
        return respond(request, "branch_new.html", {"form": form, "repo": repo})
    form = BranchForm(request.POST)
    errors = form.errors
    if not errors:
        try:
            branch = models.Branch(
                repo_key=repo.key,
                category=form.cleaned_data.get("category"),
                name=form.cleaned_data.get("name"),
                url=form.cleaned_data.get("url"),
            )
        except (db.BadValueError, ValueError) as err:
            errors["__all__"] = str(err)
    if errors:
        return respond(request, "branch_new.html", {"form": form, "repo": repo})
    branch.repo_name = repo.name
    branch.put()
    return HttpResponseRedirect(reverse(repos))


@deco.login_required
@deco.xsrf_required
def branch_edit(request, branch_id):
    """/branch_edit/<branch> - Edit a Branch record."""
    branch = models.Branch.get_by_id(int(branch_id))
    if branch.owner != request.user:
        return HttpTextResponse("You do not own this branch", status=403)
    if request.method != "POST":
        form = BranchForm(
            initial={
                "category": branch.category,
                "name": branch.name,
                "url": branch.url,
            }
        )
        return respond(request, "branch_edit.html", {"branch": branch, "form": form})

    form = BranchForm(request.POST)
    errors = form.errors
    if not errors:
        try:
            branch.category = form.cleaned_data.get("category")
            branch.name = form.cleaned_data.get("name")
            branch.url = form.cleaned_data.get("url")
        except (db.BadValueError, ValueError) as err:
            errors["__all__"] = str(err)
    if errors:
        return respond(request, "branch_edit.html", {"branch": branch, "form": form})
    branch.put()
    return HttpResponseRedirect(reverse(repos))


@deco.require_methods("POST")
@deco.login_required
@deco.xsrf_required
def branch_delete(request, branch_id):
    """/branch_delete/<branch> - Delete a Branch record."""
    branch = models.Branch.get_by_id(int(branch_id))
    if branch.owner != request.user:
        return HttpTextResponse("You do not own this branch", status=403)

    repo_key = branch.repo_key
    branch.key.delete()
    num_branches = models.Branch.query(models.Branch.repo_key == repo_key).count()
    if not num_branches:
        # Even if we don't own the repository?  Yes, I think so!  Empty
        # repositories have no representation on screen.
        repo_key.delete()

    return HttpResponseRedirect(reverse(repos))


### User Profiles ###


@deco.login_required
@deco.xsrf_required
def settings(request):
    account = models.Account.current_user_account
    if request.method != "POST":
        nickname = account.nickname
        default_context = account.default_context
        default_column_width = account.default_column_width
        form = SettingsForm(
            initial={
                "nickname": nickname,
                "context": default_context,
                "column_width": default_column_width,
                "notify_by_email": account.notify_by_email,
            }
        )
        return respond(request, "settings.html", {"form": form})
    form = SettingsForm(request.POST)
    if form.is_valid():
        account.nickname = form.cleaned_data.get("nickname")
        account.default_context = form.cleaned_data.get("context")
        account.default_column_width = form.cleaned_data.get("column_width")
        account.notify_by_email = form.cleaned_data.get("notify_by_email")
        account.fresh = False
        account.put()
    else:
        return respond(request, "settings.html", {"form": form})
    return HttpResponseRedirect(reverse(mine))


@deco.require_methods("POST")
@deco.login_required
@deco.xsrf_required
def account_delete(_request):
    account = models.Account.current_user_account
    account.key.delete()
    return HttpResponseRedirect(users.create_logout_url(reverse(index)))


@deco.login_required
@deco.xsrf_required
def migrate_entities(request):
    """Migrates entities from the specified user to the signed in user."""
    msg = None
    if request.method == "POST":
        form = MigrateEntitiesForm(request.POST)
        form.set_user(request.user)
        if form.is_valid():
            # verify that the account belongs to the user
            old_account = form.cleaned_data["account"]
            old_account_key = str(old_account.key)
            new_account_key = str(models.Account.current_user_account.key)
            for kind in ("Issue", "Repository", "Branch"):
                taskqueue.add(
                    url=reverse(task_migrate_entities),
                    params={
                        "kind": kind,
                        "old": old_account_key,
                        "new": new_account_key,
                    },
                    queue_name="migrate-entities",
                )
            msg = (
                "Migration job started. The issues, repositories and branches"
                " created with your old account (%s) will be moved to your"
                " current account (%s) in a background task and should"
                " be visible for your current account shortly."
                % (old_account.user.email(), request.user.email())
            )
    else:
        form = MigrateEntitiesForm()
    return respond(request, "migrate_entities.html", {"form": form, "msg": msg})


@deco.task_queue_required("migrate-entities")
def task_migrate_entities(request):
    """/restricted/tasks/migrate_entities - Migrates entities from one account to
  another.
  """
    kind = request.POST.get("kind")
    old = request.POST.get("old")
    new = request.POST.get("new")
    batch_size = 20
    if kind is None or old is None or new is None:
        logging.warning("Missing parameters")
        return HttpResponse()
    if kind not in ("Issue", "Repository", "Branch"):
        logging.warning("Invalid kind: %s" % kind)
        return HttpResponse()
    old_account = ndb.Key(models.Account, old).get()
    new_account = ndb.Key(models.Account, new).get()
    if old_account is None or new_account is None:
        logging.warning("Invalid accounts")
        return HttpResponse()
    # make sure that accounts match
    if old_account.user.user_id() != new_account.user.user_id():
        logging.warning("Accounts don't match")
        return HttpResponse()
    model = getattr(models, kind)
    encoded_key = request.POST.get("key")
    model_cls = model.__class__
    query = model.query(model_cls.owner == old_account.user).order(model_cls.key)
    if encoded_key:
        query = query.filter(model_cls.key > ndb.Key(urlsafe=encoded_key))
    tbd = []
    for entity in query.fetch(batch_size):
        entity.owner = new_account.user
        tbd.append(entity)
    if tbd:
        ndb.put_multi(tbd)
        taskqueue.add(
            url=reverse(task_migrate_entities),
            params={"kind": kind, "old": old, "new": new, "key": str(tbd[-1].key)},
            queue_name="migrate-entities",
        )
    return HttpResponse()


@deco.user_key_required
def user_popup(request):
    """/user_popup - Pop up to show the user info."""
    try:
        return _user_popup(request)
    except Exception as err:
        logging.exception("Exception in user_popup processing:")
        # Return HttpResponse because the JS part expects a 200 status code.
        return HttpHtmlResponse(
            '<font color="red">Error: %s; please report!</font>'
            % err.__class__.__name__
        )


def _user_popup(request):
    user = request.user_to_show
    popup_html = memcache.get("user_popup:" + user.email())
    if popup_html is None:
        num_issues_created = models.Issue.query(
            models.Issue.closed == False, models.Issue.owner == user
        ).count()
        num_issues_reviewed = models.Issue.query(
            models.Issue.closed == False, models.Issue.reviewers == user.email()
        ).count()

        user.nickname = models.Account.get_nickname_for_email(user.email())
        popup_html = render_to_response(
            "user_popup.html",
            {
                "user": user,
                "num_issues_created": num_issues_created,
                "num_issues_reviewed": num_issues_reviewed,
            },
            context_instance=RequestContext(request),
        )
        # Use time expired cache because the number of issues will change over time
        memcache.add("user_popup:" + user.email(), popup_html, 60)
    return popup_html


@deco.require_methods("POST")
def incoming_mail(request, recipients):
    """/_ah/mail/(.*)

  Handle incoming mail messages.

  The issue is not modified. No reviewers or CC's will be added or removed.
  """
    try:
        _process_incoming_mail(request.raw_post_data, recipients)
    except InvalidIncomingEmailError as err:
        logging.debug(str(err))
    return HttpTextResponse("")


def _process_incoming_mail(raw_message, recipients):
    """Process an incoming email message."""
    recipients = [x[1] for x in email.utils.getaddresses([recipients])]

    incoming_msg = mail.InboundEmailMessage(raw_message)

    if "X-Google-Appengine-App-Id" in incoming_msg.original:
        raise InvalidIncomingEmailError("Mail sent by App Engine")

    # Use the subject to find the issue number.
    # Originally the tag was (issueNNN).
    # Then we changed it to be (issue NNN by WHO).
    # We want to match either of these, and we need to deal with
    # the fact that some mail readers will fold the long subject,
    # turning a single space into "\r\n ".
    # We use "issue\s*" to handle all these forms,
    # and we omit the closing ) to accept both the original and the "by WHO" form.
    subject = incoming_msg.subject or ""
    match = re.search(r"\(issue\s*(?P<id>\d+)", subject)
    if match is None:
        raise InvalidIncomingEmailError("No issue id found: %s", subject)
    issue_id = int(match.groupdict()["id"])
    issue = models.Issue.get_by_id(issue_id)
    if issue is None:
        raise InvalidIncomingEmailError("Unknown issue ID: %d" % issue_id)
    sender = email.utils.parseaddr(incoming_msg.sender)[1]

    body = None
    for _, payload in incoming_msg.bodies("text/plain"):
        # FIXME(andi): Remove this when issue 2383 is fixed.
        # 8bit encoding results in UnknownEncodingError, see
        # http://code.google.com/p/googleappengine/issues/detail?id=2383
        # As a workaround we try to decode the payload ourselves.
        if payload.encoding == "8bit" and payload.charset:
            body = payload.payload.decode(payload.charset)
        # If neither encoding not charset is set, but payload contains
        # non-ASCII chars we can't use payload.decode() because it returns
        # payload.payload unmodified. The later type cast to db.Text fails
        # with a UnicodeDecodeError then.
        elif payload.encoding is None and payload.charset is None:
            # assume utf-8 but set replace flag to go for sure.
            body = payload.payload.decode("utf-8", "replace")
        else:
            body = payload.decode()
        break
    if body is None or not body.strip():
        raise InvalidIncomingEmailError("Ignoring empty message.")
    elif len(body) > django_settings.RIETVELD_INCOMING_MAIL_MAX_SIZE:
        # see issue325, truncate huge bodies
        trunc_msg = "... (message truncated)"
        end = django_settings.RIETVELD_INCOMING_MAIL_MAX_SIZE - len(trunc_msg)
        body = body[:end]
        body += trunc_msg

    # If the subject is long, this might come wrapped into more than one line.
    subject = " ".join([x.strip() for x in subject.splitlines()])
    msg = models.Message(
        issue_key=issue.key,
        parent=issue.key,
        subject=subject,
        sender=sender,
        recipients=[x for x in recipients],
        date=datetime.datetime.now(),
        text=body,
        draft=False,
    )

    # Add sender to reviewers if needed.
    all_emails = [
        str(x).lower()
        for x in (
            [issue.owner.email()]
            + issue.reviewers
            + issue.cc
            + issue.collaborator_emails()
        )
    ]
    if sender.lower() not in all_emails:
        query = models.Account.query(models.Account.lower_email == sender.lower())
        account = query.get()
        if account is not None:
            issue.reviewers.append(account.email)  # e.g. account.email is CamelCase
        else:
            issue.reviewers.append(db.Email(sender))

    issue.calculate_updates_for(msg)
    issue.put()
    msg.put()


@deco.login_required
def xsrf_token(request):
    """/xsrf_token - Return the user's XSRF token.

  This is used by tools like git-cl that need to be able to interact with the
  site on the user's behalf.  A custom header named X-Requesting-XSRF-Token must
  be included in the HTTP request; an error is returned otherwise.
  """
    if "HTTP_X_REQUESTING_XSRF_TOKEN" not in request.META:
        return HttpTextResponse(
            "Please include a header named X-Requesting-XSRF-Token "
            "(its content doesn't matter).",
            status=400,
        )
    return HttpTextResponse(models.Account.current_user_account.get_xsrf_token())


def customized_upload_py(request):
    """/static/upload.py - Return patched upload.py with appropiate auth type and
  default review server setting.

  This is used to let the user download a customized upload.py script
  for hosted Rietveld instances.
  """
    f = open(django_settings.UPLOAD_PY_SOURCE)
    source = f.read()
    f.close()

    # When served from a Google Apps instance, the account namespace needs to be
    # switched to "Google Apps only".
    if "AUTH_DOMAIN" in request.META and request.META["AUTH_DOMAIN"] != "gmail.com":
        source = source.replace(
            'AUTH_ACCOUNT_TYPE = "GOOGLE"', 'AUTH_ACCOUNT_TYPE = "HOSTED"'
        )

    # On a non-standard instance, the default review server is changed to the
    # current hostname. This might give weird results when using versioned appspot
    # URLs (eg. 1.latest.codereview.appspot.com), but this should only affect
    # testing.
    if request.META["HTTP_HOST"] != "codereview.appspot.com":
        review_server = request.META["HTTP_HOST"]
        if request.is_secure():
            review_server = "https://" + review_server
        source = source.replace(
            'DEFAULT_REVIEW_SERVER = "codereview.appspot.com"',
            'DEFAULT_REVIEW_SERVER = "%s"' % review_server,
        )

    return HttpResponse(source, content_type="text/x-python; charset=utf-8")


@deco.task_queue_required("deltacalculation")
def task_calculate_delta(request):
    """/restricted/tasks/calculate_delta - Calculate deltas for a patchset.

  This URL is called by taskqueue to calculate deltas behind the
  scenes. Returning a HttpResponse with any 2xx status means that the
  task was finished successfully. Raising an exception means that the
  taskqueue will retry to run the task.
  """
    ps_key = request.POST.get("key")
    if not ps_key:
        logging.error("No patchset key given.")
        return HttpResponse()
    try:
        patchset = ndb.Key(urlsafe=ps_key).get()
    except (db.KindError, db.BadKeyError) as err:
        logging.error("Invalid PatchSet key %r: %s" % (ps_key, err))
        return HttpResponse()
    if patchset is None:  # e.g. PatchSet was deleted inbetween
        logging.error("Missing PatchSet key %r" % ps_key)
        return HttpResponse()
    patchset.calculate_deltas()
    return HttpResponse()


def _build_state_value(django_request, user):
    """Composes the value for the 'state' parameter.

  Packs the current request URI and an XSRF token into an opaque string that
  can be passed to the authentication server via the 'state' parameter.

  Meant to be similar to oauth2client.appengine._build_state_value.

  Args:
    django_request: Django HttpRequest object, The request.
    user: google.appengine.api.users.User, The current user.

  Returns:
    The state value as a string.
  """
    relative_path = django_request.get_full_path().encode("utf-8")
    uri = django_request.build_absolute_uri(relative_path)
    token = xsrfutil.generate_token(
        xsrf_secret_key(), user.user_id(), action_id=str(uri)
    )
    return uri + ":" + token


def _create_flow(django_request):
    """Create the Flow object.

  The Flow is calculated using mostly fixed values and constants retrieved
  from other modules.

  Args:
    django_request: Django HttpRequest object, The request.

  Returns:
    oauth2client.client.OAuth2WebServerFlow object.
  """
    redirect_path = reverse(oauth2callback)
    redirect_uri = django_request.build_absolute_uri(redirect_path)
    client_id, client_secret, _ = auth_utils.SecretKey.get_config()
    return OAuth2WebServerFlow(
        client_id,
        client_secret,
        auth_utils.EMAIL_SCOPE,
        redirect_uri=redirect_uri,
        approval_prompt="force",
    )


def _validate_port(port_value):
    """Makes sure the port value is valid and can be used by a non-root user.

  Args:
    port_value: Integer or string version of integer.

  Returns:
    Integer version of port_value if valid, otherwise None.
  """
    try:
        port_value = int(port_value)
    except (ValueError, TypeError):
        return None

    if not (1024 <= port_value <= 49151):
        return None

    return port_value


@deco.login_required
def get_access_token(request):
    """/get-access-token - Facilitates OAuth 2.0 dance for client.

  Meant to take a 'port' query parameter and redirect to localhost with that
  port and the user's access token appended.
  """
    user = request.user
    flow = _create_flow(request)

    flow.params["state"] = _build_state_value(request, user)
    credentials = StorageByKeyName(
        CredentialsNDBModel, user.user_id(), "credentials"
    ).get()

    authorize_url = flow.step1_get_authorize_url()
    redirect_response_object = HttpResponseRedirect(authorize_url)
    if credentials is None or credentials.invalid:
        return redirect_response_object

    # Find out if credentials is expired
    refresh_failed = False
    if credentials.access_token is None or credentials.access_token_expired:
        try:
            credentials.refresh(httplib2.Http())
        except AccessTokenRefreshError:
            return redirect_response_object
        except:
            refresh_failed = True

    port_value = _validate_port(request.GET.get("port"))
    if port_value is None:
        return HttpTextResponse("Access Token: %s" % (credentials.access_token,))

    # Send access token along to localhost client
    redirect_template_args = {"port": port_value}
    if refresh_failed:
        quoted_error = urllib.parse.quote(OAUTH_DEFAULT_ERROR_MESSAGE)
        redirect_template_args["error"] = quoted_error
        client_uri = ACCESS_TOKEN_FAIL_REDIRECT_TEMPLATE % redirect_template_args
    else:
        quoted_access_token = urllib.parse.quote(credentials.access_token)
        redirect_template_args["token"] = quoted_access_token
        client_uri = ACCESS_TOKEN_REDIRECT_TEMPLATE % redirect_template_args

    return HttpResponseRedirect(client_uri)


@deco.login_required
def oauth2callback(request):
    """/oauth2callback - Callback handler for OAuth 2.0 redirect.

  Handles redirect and moves forward to the rest of the application.
  """
    error = request.GET.get("error")
    if error:
        error_msg = request.GET.get("error_description", error)
        return HttpTextResponse(
            "The authorization request failed: %s" % _safe_html(error_msg)
        )
    else:
        user = request.user
        flow = _create_flow(request)
        credentials = flow.step2_exchange(request.GET)
        StorageByKeyName(CredentialsNDBModel, user.user_id(), "credentials").put(
            credentials
        )
        redirect_uri = _parse_state_value(str(request.GET.get("state")), user)
        return HttpResponseRedirect(redirect_uri)


@deco.admin_required
def set_client_id_and_secret(request):
    """/restricted/set-client-id-and-secret - Allows admin to set Client ID and
  Secret.

  These values, from the Google APIs console, are required to validate
  OAuth 2.0 tokens within auth_utils.py.
  """
    if request.method == "POST":
        form = ClientIDAndSecretForm(request.POST)
        if form.is_valid():
            client_id = form.cleaned_data["client_id"]
            client_secret = form.cleaned_data["client_secret"]
            additional_client_ids = form.cleaned_data["additional_client_ids"]
            auth_utils.SecretKey.set_config(
                client_id, client_secret, additional_client_ids
            )
        return HttpResponseRedirect(reverse(set_client_id_and_secret))
    else:
        form = ClientIDAndSecretForm()
        return respond(request, "set_client_id_and_secret.html", {"form": form})


### Statistics.


DATE_FORMAT = "%Y-%m-%d"


def update_stats(request):
    """Endpoint that will trigger a taskqueue to update the score of all
  AccountStatsBase derived entities.
  """
    if IS_DEV:
        # Sadly, there is no way to know the admin port.
        dashboard = "http://%s:8000/taskqueue" % os.environ["SERVER_NAME"]
    else:
        # Do not use app_identity.get_application_id() since we need the 's~'.
        appid = os.environ["APPLICATION_ID"]
        versionid = os.environ["CURRENT_VERSION_ID"]
        dashboard = (
            "https://appengine.google.com/queues?queue_name=update-stats&"
            "app_id=%s&version_id=%s&" % (appid, versionid)
        )
    msg = ""
    if request.method != "POST":
        form = UpdateStatsForm()
        return respond(
            request,
            "admin_update_stats.html",
            {"form": form, "dashboard": dashboard, "msg": msg},
        )

    form = UpdateStatsForm(request.POST)
    if not form.is_valid():
        form = UpdateStatsForm()
        msg = "Invalid form data."
        return respond(
            request,
            "admin_update_stats.html",
            {"form": form, "dashboard": dashboard, "msg": msg},
        )

    tasks_to_trigger = form.cleaned_data["tasks_to_trigger"].split(",")
    tasks_to_trigger = [
        _f for _f in (t.strip().lower() for t in tasks_to_trigger) if _f
    ]
    today = datetime.datetime.utcnow().date()

    tasks = []
    if not tasks_to_trigger:
        msg = "No task to trigger."
    # Special case 'refresh'.
    elif len(tasks_to_trigger) == 1 and tasks_to_trigger[0] in ("destroy", "refresh"):
        taskqueue.add(
            url=reverse(task_refresh_all_stats_score),
            params={"destroy": str(int(tasks_to_trigger[0] == "destroy"))},
            queue_name="refresh-all-stats-score",
        )
        msg = "Triggered %s." % tasks_to_trigger[0]
    else:
        tasks = []
        for task in tasks_to_trigger:
            if task in ("monthly", "30"):
                tasks.append(task)
            elif models.verify_account_statistics_name(task):
                if task.count("-") == 2:
                    tasks.append(task)
                else:
                    # It's a month. Add every single day of the month as long as it's
                    # before today.
                    year, month = list(map(int, task.split("-")))
                    days = calendar.monthrange(year, month)[1]
                    tasks.extend(
                        "%s-%02d" % (task, d + 1)
                        for d in range(days)
                        if datetime.date(year, month, d + 1) < today
                    )
            else:
                msg = "Invalid item."
                break
        else:
            if len(set(tasks)) != len(tasks):
                msg = "Duplicate items found."
            else:
                taskqueue.add(
                    url=reverse(task_update_stats),
                    params={"tasks": json.dumps(tasks), "date": str(today)},
                    queue_name="update-stats",
                )
                msg = "Triggered the following tasks: %s." % ", ".join(tasks)
    logging.info(msg)
    return respond(
        request,
        "admin_update_stats.html",
        {"form": form, "dashboard": dashboard, "msg": msg},
    )


def cron_update_yesterday_stats(_request):
    """Daily cron job to trigger all the necessary task queue.

  - Triggers a task to update daily summaries.
  - This task will then trigger a task to update rolling summaries.
  - This task will then trigger a task to update monthly summaries.

  Using 3 separate tasks to space out datastore contention and reduces the
  scope of each task so the complete under 10 minutes, making retries softer
  on the system when the datastore throws exceptions or the load for the day
  is high.
  """
    today = datetime.datetime.utcnow().date()
    day = str(today - datetime.timedelta(days=1))
    tasks = [day, "30", "monthly"]
    taskqueue.add(
        url=reverse(task_update_stats),
        params={"tasks": json.dumps(tasks), "date": str(today)},
        queue_name="update-stats",
    )
    out = "Triggered tasks for day %s: %s" % (day, ", ".join(tasks))
    logging.info(out)
    return HttpTextResponse(out)


def figure_out_real_accounts(people_involved, people_caches):
    """Removes people that are known to be role accounts or mailing lists.

  Sadly, Account instances are created even for mailing lists (!) but mailing
  lists never create an issue, so assume that a reviewer that never created an
  issue is a nobody.

  Arguments:
    people_involved: set or list of email addresses to scan.
    people_caches: a lookup cache of already resolved email addresses.

  Returns:
    list of the email addresses that are not nobodies.
  """
    # Using '+' as a filter removes a fair number of WATCHLISTS entries.
    people_involved = set(
        i
        for i in people_involved
        if (
            "+" not in i
            and not i.startswith("commit-bot")
            and not i.endswith("gserviceaccount.com")
        )
    )
    people_involved -= people_caches["fake"]

    # People we are still unsure about that need to be looked up.
    people_to_look_for = list(people_involved - people_caches["real"])

    futures = [
        models.Issue.query(models.Issue.owner == users.User(r)).fetch(
            limit=1, keys_only=True
        )
        for r in people_to_look_for
    ]
    for i, future in enumerate(futures):
        account_email = people_to_look_for[i]
        if not list(future):
            people_caches["fake"].add(account_email)
            people_involved.remove(account_email)
        else:
            people_caches["real"].add(account_email)
    return people_involved


def search_relevant_first_email_for_user(issue_owner, messages, user, people_caches):
    """Calculates which Message is representative for the request latency for this
  review for this user.

  Returns:
  - index in |messages| that is the most representative for this user as a
    reviewer or None if no Message is relevant at all. In that case, the caller
    should fall back to Issue.created.
  - bool if it looks like a drive-by.

  It is guaranteed that the index returned is a Message sent either by
  |issue_owner| or |user|.
  """
    # Shortcut. No need to calculate the value.
    if issue_owner == user:
        return None, False

    # Search for the first of:
    # - message by the issue owner sent to the user or to mailing lists.
    # - message by the user, for DRIVE_BY and NOT_REQUESTED.
    # Otherwise, return None.
    last_owner_message_index = None
    for i, m in enumerate(messages):
        if m.sender == issue_owner:
            last_owner_message_index = i
            if user in m.recipients:
                return i, False
            # Detect the use case where a request for review is sent to a mailing list
            # and a random reviewer picks it up. We don't want to downgrade the
            # reviewer from a proper review down to DRIVE_BY, so mark it as the
            # important message for everyone. A common usecase is code reviews on
            # golang-dev@googlegroups.com.
            recipients = set(m.recipients) - set([m.sender, issue_owner])
            if not figure_out_real_accounts(recipients, people_caches):
                return i, False
        elif m.sender == user:
            # The issue owner didn't send a request specifically to this user but the
            # dude replied anyway. It can happen if the user was on the cc list with
            # user+cc@example.com. In that case, use the last issue owner email.
            # We want to use this message for latency calculation DRIVE_BY and
            # NOT_REQUESTED.
            if last_owner_message_index is not None:
                return last_owner_message_index, True
            # issue_owner is MIA.
            return i, True
        else:
            # Maybe a reviewer added 'user' on the review on its behalf. Likely
            # m.sender wants to defer the review to someone else.
            if user in m.recipients:
                return i, False
    # Sends the last Message index if there is any.
    return last_owner_message_index, False


def process_issue(
    start, day_to_process, message_index, drive_by, issue_owner, messages, user
):
    """Calculates 'latency', 'lgtms' and 'review_type' for a reviewer on an Issue.

  Arguments:
  - start: moment to use to calculate the latency. Can be either the moment a
           Message was sent or Issue.created if no other signal exists.
  - day_to_process: the day to look for for new 'events'.
  - message_index: result of search_relevant_first_email_for_user().
  - drive_by: the state of things looks like a DRIVE_BY or a NOT_REQUESTED.
  - issue_owner: shortcut for issue.owner.email().
  - messages: shortcut for issue.messages sorted by date. Cannot be empty.
  - user: user to calculate latency.

  A Message must have been sent on day_to_process that would imply data,
  otherwise None, None is returned.
  """
    assert isinstance(start, datetime.datetime), start
    assert isinstance(day_to_process, datetime.date), day_to_process
    assert message_index is None or 0 <= message_index < len(messages), message_index
    assert drive_by in (True, False), drive_by
    assert issue_owner.count("@") == 1, issue_owner
    assert all(isinstance(m, models.Message) for m in messages), messages
    assert user.count("@") == 1, user

    lgtms = sum(
        m.sender == user
        and m.find(models.Message.LGTM_RE, owner_allowed=True)
        and not m.find(models.Message.NOT_LGTM_RE, owner_allowed=True)
        for m in messages
    )

    # TODO(maruel): Check for the base username part, e.g.:
    # if user.split('@', 1)[0] == issue_owner.split('@', 1)[0]:
    # For example, many people have both matching @google.com and @chromium.org
    # accounts.
    if user == issue_owner:
        if not any(m.date.date() == day_to_process for m in messages):
            return -1, None, None
        # There's no concept of review latency for OUTGOING reviews.
        return -1, lgtms, models.AccountStatsBase.OUTGOING

    if message_index is None:
        # Neither issue_owner nor user sent an email, ignore.
        return -1, None, None

    if drive_by:
        # Tricky case. Need to determine the difference between NOT_REQUESTED and
        # DRIVE_BY. To determine if an issue is NOT_REQUESTED, look if the owner
        # never sent a request for review in the previous messages.
        review_type = (
            models.AccountStatsBase.NOT_REQUESTED
            if messages[message_index].sender == user
            else models.AccountStatsBase.DRIVE_BY
        )
    else:
        review_type = models.AccountStatsBase.NORMAL

    for m in messages[message_index:]:
        if m.sender == user:
            if m.date.date() < day_to_process:
                # It was already updated on a previous day. Skip calculation.
                return -1, None, None
            return int((m.date - start).total_seconds()), lgtms, review_type

    # 'user' didn't send a message, so no latency can be calculated.
    assert not lgtms, lgtms
    return -1, lgtms, models.AccountStatsBase.IGNORED


def yield_people_issue_to_update(day_to_process, issues, messages_looked_up):
    """Yields all the combinations of user-day-issue that needs to be updated.

  Arguments:
  - issues: set() of all the Issue touched.
  - messages_looked_up: list of one int to count the number of Message looked
    up.

  Yields:
   - tuple user, day, issue_id, latency, lgtms, review_type.
  """
    assert isinstance(day_to_process, datetime.datetime), day_to_process
    assert not issues and isinstance(issues, set), issues
    assert [0] == messages_looked_up, messages_looked_up

    day_to_process_date = day_to_process.date()
    # Cache people that are valid accounts or not to reduce datastore lookups.
    people_caches = {"fake": set(), "real": set()}
    # dict((user, day) -> set(issue_id)) mapping of
    # the AccountStatsDay that will need to be recalculated.
    need_to_update = {}
    # TODO(maruel): Use asynchronous programming to start moving on to the next
    # issue right away. This means creating our own Future instances.

    cursor = None
    while True:
        query = models.Message.query(
            models.Message.date >= day_to_process,
            default_options=ndb.QueryOptions(keys_only=True),
        ).order(models.Message.date)
        # Someone sane would ask: why the hell do this? I don't know either but
        # that's the only way to not have it throw an exception after 60 seconds.
        message_keys, cursor, more = query.fetch_page(100, start_cursor=cursor)
        if not message_keys:
            # We're done, no more cursor.
            break
        for message_key in message_keys:
            # messages_looked_up may be overcounted, as the messages on the next day
            # on issues already processed will be accepted as valid, until a new issue
            # is found.
            messages_looked_up[0] += 1
            issue_key = message_key.parent()
            issue_id = issue_key.id()
            if issue_id in issues:
                # This issue was already processed.
                continue

            # Aggressively fetch data concurrently.
            message_future = message_key.get_async()
            issue_future = issue_key.get_async()
            messages_future = models.Message.query(ancestor=issue_key).fetch_async(
                batch_size=1000
            )
            if message_future.get_result().date.date() > day_to_process_date:
                # Now on the next day. It is important to stop, especially when looking
                # at very old CLs.
                messages_looked_up[0] -= 1
                cursor = None
                break

            # Make sure to not process this issue a second time.
            issues.add(issue_id)
            issue = issue_future.get_result()
            # Sort manually instead of using .order('date') to save one index. Strips
            # off any Message after day_to_process.
            messages = sorted(
                (
                    m
                    for m in messages_future.get_result()
                    if m.date.date() <= day_to_process_date
                ),
                key=lambda x: x.date,
            )

            # Updates the dict of the people-day pairs that will need to be updated.
            issue_owner = issue.owner.email()
            # Ignore issue.reviewers since it can change over time. Sadly m.recipients
            # also contains people cc'ed so take care of these manually.
            people_to_consider = set(m.sender for m in messages)
            people_to_consider.add(issue_owner)
            for m in messages:
                for r in m.recipients:
                    if (
                        any(n.sender == r for n in messages)
                        or r in issue.reviewers
                        or r not in issue.cc
                    ):
                        people_to_consider.add(r)

            # 'issue_owner' is by definition a real account. Save one datastore
            # lookup.
            people_caches["real"].add(issue_owner)

            for user in figure_out_real_accounts(people_to_consider, people_caches):
                message_index, drive_by = search_relevant_first_email_for_user(
                    issue_owner, messages, user, people_caches
                )
                if message_index == None or (
                    drive_by
                    and messages[message_index].sender == user
                    and not any(
                        m.sender == issue_owner for m in messages[:message_index]
                    )
                ):
                    # There's no important message, calculate differently by using the
                    # issue creation date.
                    start = issue.created
                else:
                    start = messages[message_index].date

                # Note that start != day_to_process_date
                start_str = str(start.date())
                user_issue_set = need_to_update.setdefault((user, start_str), set())
                if not issue_id in user_issue_set:
                    user_issue_set.add(issue_id)
                    latency, lgtms, review_type = process_issue(
                        start,
                        day_to_process_date,
                        message_index,
                        drive_by,
                        issue_owner,
                        messages,
                        user,
                    )
                    if review_type is None:
                        # process_issue() determined there is nothing to update.
                        continue
                    yield user, start_str, issue_id, latency, lgtms, review_type
        if not cursor:
            break


@deco.task_queue_required("update-stats")
def task_update_stats(request):
    """Dispatches the relevant task to execute.

  Can dispatch either update_daily_stats, update_monthly_stats or
  update_rolling_stats.
  """
    tasks = json.loads(request.POST.get("tasks"))
    date_str = request.POST.get("date")
    cursor = ndb.Cursor(urlsafe=request.POST.get("cursor"))
    countdown = 15
    if not tasks:
        msg = "Nothing to execute!?"
        logging.warning(msg)
        out = HttpTextResponse(msg)
    else:
        # Dispatch the task to execute.
        task = tasks.pop(0)
        logging.info("Running %s.", task)
        if task.count("-") == 2:
            out, cursor = update_daily_stats(
                cursor, datetime.datetime.strptime(task, DATE_FORMAT)
            )
        elif task == "monthly":
            # The only reason day is used is in case a task queue spills over the next
            # day.
            day = datetime.datetime.strptime(date_str, DATE_FORMAT)
            out, cursor = update_monthly_stats(cursor, day)
        elif task == "30":
            yesterday = (
                datetime.datetime.strptime(date_str, DATE_FORMAT)
                - datetime.timedelta(days=1)
            ).date()
            out, cursor = update_rolling_stats(cursor, yesterday)
        else:
            msg = "Unknown task %s, ignoring." % task
            cursor = ""
            logging.error(msg)
            out = HttpTextResponse(msg)

        if cursor:
            # Not done yet!
            tasks.insert(0, task)
            countdown = 0

    if out.status_code == 200 and tasks:
        logging.info("%d tasks to go!\n%s", len(tasks), ", ".join(tasks))
        # Space out the task queue execution by 15s to reduce the risk of
        # datastore inconsistency to get in the way, since no transaction is used.
        # This means to process a full month, it'll include 31*15s = 7:45 minutes
        # delay. 15s is not a lot but we are in an hurry!
        taskqueue.add(
            url=reverse(task_update_stats),
            params={
                "tasks": json.dumps(tasks),
                "date": date_str,
                "cursor": cursor.urlsafe() if cursor else "",
            },
            queue_name="update-stats",
            countdown=countdown,
        )
    return out


def update_daily_stats(cursor, day_to_process):
    """Updates the statistics about every reviewer for the day.

  Note that joe@google != joe@chromium, so make sure to always review with the
  right email address or your stats will suffer.

  The goal here is:
  - detect all the active reviewers in the past day.
  - for each of them, update their statistics for the past day.

  There can be thousands of CLs modified in a single day so throughput
  efficiency is important here, as it has only 10 minutes to complete.
  """
    start = time.time()
    # Look at all messages sent in the day. The issues associated to these
    # messages are the issues we care about.
    issues = set()
    # Use a list so it can be modified inside the generator.
    messages_looked_up = [0]
    total = 0
    try:
        chunk_size = 10
        max_futures = 200
        futures = []
        items = []
        for packet in yield_people_issue_to_update(
            day_to_process, issues, messages_looked_up
        ):
            user, day, issue_id, latency, lgtms, review_type = packet
            account_key = ndb.Key("Account", models.Account.get_id_for_email(user))
            found = False
            for item in items:
                # A user could touch multiple issues in a single day.
                if item.key.id() == day and item.key.parent() == account_key:
                    found = True
                    break
            else:
                # Find the object and grab it. Do not use get_or_insert() to save a
                # transaction and double-write.
                item = models.AccountStatsDay.get_by_id(
                    day, parent=account_key, use_cache=False
                )
                if not item:
                    # Create a new one.
                    item = models.AccountStatsDay(id=day, parent=account_key)

            if issue_id in item.issues:
                # It was already there, update.
                i = item.issues.index(issue_id)

                if (
                    item.latencies[i] == latency
                    and item.lgtms[i] == lgtms
                    and item.review_types[i] == review_type
                ):
                    # Was already calculated, skip.
                    continue

                # Make sure to not "downgrade" the object.
                if item.lgtms[i] > lgtms:
                    # Never lower the number of lgtms.
                    continue

                if item.latencies[i] >= 0 and latency == -1:
                    # Unchanged or "lower priority", no need to store again.
                    continue

                if (
                    item.latencies[i] >= 0
                    and latency >= 0
                    and item.latencies[i] != latency
                ):
                    # That's rare, the new calculated latency doesn't match the previously
                    # calculated latency. File an error but let it go.
                    logging.error(
                        "New calculated latency doesn't match previously calculated "
                        "value.\n%s != %s\nItem %d in:\n%s",
                        item.latencies[i],
                        latency,
                        i,
                        item,
                    )

                item.latencies[i] = latency
                item.lgtms[i] = lgtms
                item.review_types[i] = review_type
            else:
                # TODO(maruel): Sort?
                item.issues.append(issue_id)
                item.latencies.append(latency)
                item.lgtms.append(lgtms)
                item.review_types.append(review_type)

            if not found:
                items.append(item)
                if len(items) == chunk_size:
                    futures.extend(ndb.put_multi_async(items, use_cache=False))
                    total += chunk_size
                    items = []
                    futures = [f for f in futures if not f.done()]
                    while len(futures) > max_futures:
                        # Slow down to limit memory usage.
                        ndb.Future.wait_any(futures)
                        futures = [f for f in futures if not f.done()]

        if items:
            futures.extend(ndb.put_multi_async(items, use_cache=False))
            total += len(items)
        ndb.Future.wait_all(futures)
        result = 200
    except (db.Timeout, DeadlineExceededError):
        result = 500

    out = ("%s\n" "%d messages\n" "%d issues\n" "Updated %d items\n" "In %.1fs\n") % (
        day_to_process.date(),
        messages_looked_up[0],
        len(issues),
        total,
        time.time() - start,
    )
    if result == 200:
        logging.info(out)
    else:
        logging.error(out)
    return HttpTextResponse(out, status=result), ""


def update_rolling_stats(cursor, reference_day):
    """Looks at all accounts and recreates all the rolling 30 days
  AccountStatsMulti summaries.

  Note that during the update, the leaderboard will be inconsistent.

  Only do 1000 accounts at a time since there's a memory leak in the function.
  """
    assert isinstance(cursor, ndb.Cursor), cursor
    assert isinstance(reference_day, datetime.date), reference_day
    start = time.time()
    total = 0
    total_deleted = 0
    try:
        # Process *all* the accounts.
        duration = "30"
        chunk_size = 10
        futures = []
        items = []
        to_delete = []
        accounts = 0
        while True:
            query = models.Account.query()
            account_keys, next_cursor, more = query.fetch_page(
                100, keys_only=True, start_cursor=cursor
            )
            if not account_keys:
                # We're done, no more cursor.
                next_cursor = None
                break

            a_key = ""
            for a_key in account_keys:
                accounts += 1
                # TODO(maruel): If date of each issue was saved in the entity, this
                # would not be necessary, assuming the entity doesn't become itself
                # corrupted.
                rolling_future = models.AccountStatsMulti.get_by_id_async(
                    duration, parent=a_key
                )
                days = [
                    str(reference_day - datetime.timedelta(days=i))
                    for i in range(int(duration))
                ]
                days_keys = [
                    ndb.Key(models.AccountStatsDay, d, parent=a_key) for d in days
                ]
                valid_days = [_f for _f in ndb.get_multi(days_keys) if _f]
                if not valid_days:
                    rolling = rolling_future.get_result()
                    if rolling:
                        to_delete.append(rolling.key)
                        if len(to_delete) == chunk_size:
                            futures.extend(ndb.delete_multi_async(to_delete))
                            total_deleted += chunk_size
                            to_delete = []
                            futures = [f for f in futures if not f.done()]
                    continue

                # Always override the content.
                rolling = models.AccountStatsMulti(id=duration, parent=a_key)
                # Sum all the daily instances into the rolling summary. Always start
                # over because it's not just adding data, it's also removing data from
                # the day that got excluded from the rolling summary.
                if models.sum_account_statistics(rolling, valid_days):
                    items.append(rolling)
                    if len(items) == chunk_size:
                        futures.extend(ndb.put_multi_async(items))
                        total += chunk_size
                        items = []
                        futures = [f for f in futures if not f.done()]

            if accounts == 1000 or (time.time() - start) > 300:
                # Limit memory usage.
                logging.info("%d accounts, last was %s", accounts, a_key.id()[1:-1])
                break

        if items:
            futures.extend(ndb.put_multi_async(items))
            total += len(items)
        if to_delete:
            futures.extend(ndb.delete_multi_async(to_delete))
            total_deleted += len(to_delete)
        ndb.Future.wait_all(futures)
        result = 200
    except (db.Timeout, DeadlineExceededError):
        result = 500

    out = "%s\nLooked up %d accounts\nStored %d items\nDeleted %d\nIn %.1fs\n" % (
        reference_day,
        accounts,
        total,
        total_deleted,
        time.time() - start,
    )
    if result == 200:
        logging.info(out)
    else:
        logging.error(out)
    return HttpTextResponse(out, status=result), next_cursor


def update_monthly_stats(cursor, day_to_process):
    """Looks at all AccountStatsDay instance updated on that day and updates the
  corresponding AccountStatsMulti instance.

  This taskqueue updates all the corresponding monthly AccountStatsMulti
  summaries by looking at all AccountStatsDay.modified.
  """
    today = datetime.datetime.utcnow().date()
    start = time.time()
    total = 0
    skipped = 0
    try:
        # The biggest problem here is not time but memory usage so limit the number
        # of ongoing futures.
        max_futures = 200
        futures = []
        days_stats_fetched = 0
        yielded = 0
        q = models.AccountStatsDay.query(
            models.AccountStatsDay.modified >= day_to_process,
            default_options=ndb.QueryOptions(keys_only=True),
        )
        months_to_regenerate = set()
        while True:
            day_stats_keys, cursor, more = q.fetch_page(100, start_cursor=cursor)
            if not day_stats_keys:
                cursor = None
                break
            days_stats_fetched += len(day_stats_keys)
            if not (days_stats_fetched % 1000):
                logging.info("Scanned %d AccountStatsDay.", days_stats_fetched)

            # Create a batch of items to process.
            batch = []
            for key in day_stats_keys:
                month_name = key.id().rsplit("-", 1)[0]
                account_name = key.parent().id()
                lookup_key = "%s-%s" % (month_name, account_name)
                if not lookup_key in months_to_regenerate:
                    batch.append((month_name, account_name))
                months_to_regenerate.add(lookup_key)

            for month_name, account_id in batch:
                yielded += 1
                if not (yielded % 1000):
                    logging.info(
                        "%d items done, %d skipped, %d yielded %d futures.",
                        total,
                        skipped,
                        yielded,
                        len(futures),
                    )

                account_key = ndb.Key(models.Account, account_id)
                monthly = models.AccountStatsMulti.get_by_id(
                    month_name, parent=account_key, use_cache=False
                )
                if not monthly:
                    # Create a new one.
                    monthly = models.AccountStatsMulti(
                        id=month_name, parent=account_key
                    )
                elif monthly.modified.date() == today:
                    # It was modified today, skip it.
                    skipped += 1
                    continue

                days_in_month = calendar.monthrange(
                    *list(map(int, month_name.split("-")))
                )[1]
                days_name = [
                    month_name + "-%02d" % (i + 1) for i in range(days_in_month)
                ]
                days_keys = [
                    ndb.Key(models.AccountStatsDay, d, parent=account_key)
                    for d in days_name
                ]
                days = [d for d in ndb.get_multi(days_keys, use_cache=False) if d]
                assert days, (month_name, account_id)
                if models.sum_account_statistics(monthly, days):
                    futures.extend(ndb.put_multi_async([monthly], use_cache=False))
                    total += 1
                    while len(futures) > max_futures:
                        # Slow down to limit memory usage.
                        ndb.Future.wait_any(futures)
                        futures = [f for f in futures if not f.done()]
                else:
                    skipped += 1

            if (time.time() - start) > 400:
                break

        ndb.Future.wait_all(futures)
        result = 200
    except (db.Timeout, DeadlineExceededError) as e:
        logging.error(str(e))
        result = 500

    out = "%s\nStored %d items\nSkipped %d\nIn %.1fs\n" % (
        day_to_process.date(),
        total,
        skipped,
        time.time() - start,
    )
    if result == 200:
        logging.info(out)
    else:
        logging.error(out)
    return HttpTextResponse(out, status=result), cursor


@deco.task_queue_required("refresh-all-stats-score")
def task_refresh_all_stats_score(request):
    """Updates all the scores or destroy them all.

  - Updating score is necessary when models.compute_score() is changed.
  - Destroying the instances is necessary if
    search_relevant_first_email_for_user() or process_issue() are modified.
  """
    start = time.time()
    cls_name = request.POST.get("cls") or "Day"
    destroy = int(request.POST.get("destroy", "0"))
    cursor = datastore_query.Cursor(urlsafe=request.POST.get("cursor"))
    task_count = int(request.POST.get("task_count", "0"))
    assert cls_name in ("Day", "Multi"), cls_name
    cls = models.AccountStatsDay if cls_name == "Day" else models.AccountStatsMulti

    # Task queues are given 10 minutes. Do it in 9 minutes chunks to protect
    # against most timeout conditions.
    timeout = 540
    updated = 0
    skipped = 0
    try:
        futures = []
        chunk_size = 10
        items = []
        more = True
        if destroy:
            options = ndb.QueryOptions(keys_only=True)
        else:
            options = ndb.QueryOptions()
        while more:
            batch, cursor, more = cls.query(default_options=options).fetch_page(
                20, start_cursor=cursor
            )
            if destroy:
                futures.extend(ndb.delete_multi_async(batch))
                updated += len(batch)
            else:
                for i in batch:
                    score = models.compute_score(i)
                    if i.score != score:
                        items.append(i)
                        if len(items) == chunk_size:
                            futures.extend(ndb.put_multi_async(items))
                            updated += chunk_size
                            items = []
                            futures = [f for f in futures if not f.done()]
                    else:
                        skipped += 1
            if time.time() - start >= timeout:
                break
        if items:
            futures.extend(ndb.put_multi_async(items))
            updated += chunk_size
        ndb.Future.wait_all(futures)
        if not more and cls_name == "Day":
            # Move to the Multi instances.
            more = True
            cls_name = "Multi"
            cursor = datastore_query.Cursor()
        if more:
            taskqueue.add(
                url=reverse(task_refresh_all_stats_score),
                params={
                    "cls": cls_name,
                    "cursor": cursor.urlsafe() if cursor else "",
                    "destroy": str(destroy),
                    "task_count": str(task_count + 1),
                },
                queue_name="refresh-all-stats-score",
            )
        result = 200
    except (db.Timeout, DeadlineExceededError):
        result = 500
    out = "Index: %d\nType = %s\nStored %d items\nSkipped %d\nIn %.1fs\n" % (
        task_count,
        cls.__name__,
        updated,
        skipped,
        time.time() - start,
    )
    if result == 200:
        logging.info(out)
    else:
        logging.error(out)
    return HttpTextResponse(out, status=result)


def quarter_to_months(when):
    """Manually handles the forms 'YYYY' or 'YYYY-QX'."""
    today = datetime.datetime.utcnow().date()
    if when.isdigit() and 2008 <= int(when) <= today.year:
        # Select the whole year.
        year = int(when)
        if year == today.year:
            out = ["%04d-%02d" % (year, i + 1) for i in range(today.month)]
        else:
            out = ["%04d-%02d" % (year, i + 1) for i in range(12)]
    else:
        quarter = re.match(r"^(\d\d\d\d-)[qQ]([1-4])$", when)
        if not quarter:
            return None
        prefix = quarter.group(1)
        # Convert the quarter into 3 months group.
        base = (int(quarter.group(2)) - 1) * 3 + 1
        out = ["%s%02d" % (prefix, i) for i in range(base, base + 3)]

    logging.info("Expanded to %s" % ", ".join(out))
    return out


def show_user_impl(user, when):
    months = None
    if not models.verify_account_statistics_name(when):
        months = quarter_to_months(when)
        if not months:
            return None

    account_key = ndb.Key(models.Account, models.Account.get_id_for_email(user))
    # Determines which entity class should be loaded by the number of '-'.
    cls = models.AccountStatsDay if when.count("-") == 2 else models.AccountStatsMulti
    if months:
        # Normalize to 'q'.
        when = when.lower()
        # Loads the stats for the 3 months and merge them.
        keys = [ndb.Key(cls, i, parent=account_key) for i in months]
        values = [_f for _f in ndb.get_multi(keys) if _f]
        stats = cls(id=when, parent=account_key)
        models.sum_account_statistics(stats, values)
    else:
        stats = cls.get_by_id(when, parent=account_key)
        if not stats:
            # It's a valid date or rolling summary key, so if there's nothing, just
            # return the fact there's no data with an empty object.
            stats = cls(id=when, parent=account_key)
    return stats


@deco.user_key_required
def show_user_stats(request, when):
    stats = show_user_impl(request.user_to_show.email(), when)
    if not stats:
        return HttpResponseNotFound()
    incoming = [
        {
            "issue": stats.issues[i],
            "latency": stats.latencies[i],
            "lgtms": stats.lgtms[i],
            "review_type": models.AccountStatsBase.REVIEW_TYPES[stats.review_types[i]],
        }
        for i in range(len(stats.issues))
        if stats.review_types[i] != models.AccountStatsBase.OUTGOING
    ]
    outgoing = [
        {"issue": stats.issues[i], "lgtms": stats.lgtms[i],}
        for i in range(len(stats.issues))
        if stats.review_types[i] == models.AccountStatsBase.OUTGOING
    ]
    return respond(
        request,
        "user_stats.html",
        {
            "viewed_account": request.user_to_show,
            "incoming": incoming,
            "outgoing": outgoing,
            "stats": stats,
            "when": when,
        },
    )


@deco.json_response
@deco.user_key_required
def show_user_stats_json(request, when):
    stats = show_user_impl(request.user_to_show.email(), when)
    if not stats:
        return {deco.STATUS_CODE: 404}
    return stats.to_dict()


def leaderboard_impl(when, limit):
    """Returns the leaderboard for this Rietveld instance on |when|.

  It returns the list of the reviewers sorted by their score for
  the past weeks, a specific day or month or a quarter.
  """
    when = when.lower()
    months = None
    if not models.verify_account_statistics_name(when):
        months = quarter_to_months(when)
        if not months:
            return None

    cls = models.AccountStatsDay if when.count("-") == 2 else models.AccountStatsMulti
    if months:
        # Use the IN operator to simultaneously select the 3 months.
        results = cls.query(cls.name.IN(months)).order(cls.score).fetch(limit)
        # Then merge all the results accordingly.
        tops = {}
        for i in results:
            tops.setdefault(i.user, []).append(i)
        for key, values in tops.items():
            values.sort(key=lambda x: x.name)
            out = models.AccountStatsMulti(id=when, parent=values[0].key.parent())
            models.sum_account_statistics(out, values)
            tops[key] = out
        tops = sorted(iter(tops.values()), key=lambda x: x.score)
    else:
        # Grabs the pre-calculated entities or daily entity.
        tops = cls.query(cls.name == when).order(cls.score).fetch(limit)

    # Remove anyone with a None score.
    return [t for t in tops if t.score is not None]


def stats_to_dict(t):
    """Adds value 'user'.

  It is a meta property so it is not included in to_dict() by default.
  """
    o = t.to_dict()
    o["user"] = t.user
    return o


@deco.json_response
def leaderboard_json(request, when):
    limit = _clean_int(request.GET.get("limit"), 300, 1, 1000)
    data = leaderboard_impl(when, limit)
    if data is None:
        return {deco.STATUS_CODE: 404}
    return [stats_to_dict(t) for t in data]


def leaderboard(request, when):
    """Prints the leaderboard for this Rietveld instance."""
    limit = _clean_int(request.GET.get("limit"), 300, 1, 1000)
    data = leaderboard_impl(when, limit)
    if data is None:
        return HttpResponseNotFound()
    tops = []
    shame = []
    for i in data:
        if i.score == models.AccountStatsBase.NULL_SCORE:
            shame.append(i)
        else:
            tops.append(i)
    return respond(
        request, "leaderboard.html", {"tops": tops, "shame": shame, "when": when}
    )
