# Copyright 2013 Google Inc.
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

"""Collection of functions to be used for Auth related activies.

The two methods to be chiefy used are:
  - get_current_user
  - is_current_user_admin

Both of these methods check for a cookie-based current user (through the Users
API) and an OAuth 2.0 current user (through the OAuth API).

In the OAuth 2.0 case, we also check that the token from the request was minted
for this specific application. This is because we have no scope of our own to
check, so must use the client ID for the token as a proxy for scope.

We use Google's email scope when using oauth.get_current_user and
oauth.is_current_user_admin, since these methods require this scope *as a
minimum* to return a users.User object (since User objects have an email
attribute). As an extra check, we also make sure that OAuth 2.0 tokens used have
been minted with *only* the email scope.

See google.appengine.ext.endpoints.users_id_token for reference:
('https://code.google.com/p/googleappengine/source/browse/trunk/python/google/'
 'appengine/ext/endpoints/users_id_token.py')
"""

import logging

from google.appengine.api import oauth
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import ndb
from google.appengine.runtime import apiproxy_errors


EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"


class SecretKey(ndb.Model):
    """Model for representing project secret keys."""

    client_id = ndb.StringProperty(required=True, indexed=False)
    client_secret = ndb.StringProperty(required=True, indexed=False)
    additional_client_ids = ndb.StringProperty(repeated=True, indexed=False)

    GLOBAL_KEY = "_global_config"

    @classmethod
    def set_config(cls, client_id, client_secret, additional_client_ids=None):
        """Sets global config object using a Client ID and Secret.

    Args:
      client_id: String containing Google APIs Client ID.
      client_secret: String containing Google APIs Client Secret.
      additional_client_ids: List of strings for Google APIs Client IDs which
        are allowed against this application but not used to mint tokens on
        the server. For example, service accounts which interact with this
        application. Defaults to None.

    Returns:
      The inserted SecretKey object.
    """
        additional_client_ids = additional_client_ids or []
        config = cls(
            id=cls.GLOBAL_KEY,
            client_id=client_id,
            client_secret=client_secret,
            additional_client_ids=additional_client_ids,
        )
        config.put()
        return config

    @classmethod
    def get_config(cls):
        """Gets tuple of Client ID and Secret from global config object.

    Returns:
      3-tuple containing the Client ID and Secret from the global config
          SecretKey object as well as a list of other allowed client IDs, if the
          config is in the datastore, else the tuple (None, None, []).
    """
        config = cls.get_by_id(cls.GLOBAL_KEY)
        if config is None:
            return None, None, []
        else:
            return (
                config.client_id,
                config.client_secret,
                config.additional_client_ids,
            )


def _get_client_id(tries=3):
    """Call oauth.get_client_id() and retry if it times out."""
    for attempt in range(tries):
        try:
            return oauth.get_client_id(EMAIL_SCOPE)
        except apiproxy_errors.DeadlineExceededError:
            logging.error("get_client_id() timed out on attempt %r", attempt)
            if attempt == tries - 1:
                raise


def get_current_rietveld_oauth_user():
    """Gets the current OAuth 2.0 user associated with a request.

  This user must be intending to reach this application, so we check the token
  info to verify this is the case.

  Returns:
    A users.User object that was retrieved from the App Engine OAuth library if
        the token is valid, otherwise None.
  """
    # TODO(dhermes): Address local environ here as well.
    try:
        current_client_id = _get_client_id()
    except oauth.Error:
        return

    accepted_client_id, _, additional_client_ids = SecretKey.get_config()
    if (
        accepted_client_id != current_client_id
        and current_client_id not in additional_client_ids
    ):
        logging.debug(
            "Client ID %r not intended for this application.", current_client_id
        )
        return

    try:
        return oauth.get_current_user(EMAIL_SCOPE)
    except oauth.Error:
        logging.warning("A Client ID was retrieved with no corresponsing user.")


def get_current_user():
    """Gets the current user associated with a request.

  First tries to verify a user with the Users API (cookie-based auth), and then
  falls back to checking for an OAuth 2.0 user with a token minted for use with
  this application.

  Returns:
    A users.User object that was retrieved from the App Engine Users or OAuth
        library if such a user can be determined, otherwise None.
  """
    current_cookie_user = users.get_current_user()
    if current_cookie_user is not None:
        return current_cookie_user
    return get_current_rietveld_oauth_user()


class AnyAuthUserProperty(ndb.UserProperty):
    """An extension of the UserProperty which also accepts OAuth users.

  The default db.UserProperty only considers cookie-based Auth users.
  """

    def _prepare_for_put(self, entity):
        """Prepare to store value to DB, including supplying a default value.

    NOTE: This is adapted from UserProperty.default_value but uses a different
    get_current_user() method.
    """
        if self._auto_current_user or (
            self._auto_current_user_add and not self._has_value(entity)
        ):
            value = get_current_user()
            if value is not None:
                self._store_value(entity, value)


def is_current_user_admin():
    """Determines if the current user associated with a request is an admin.

  First tries to verify if the user is an admin with the Users API (cookie-based
  auth), and then falls back to checking for an OAuth 2.0 admin user with a
  token minted for use with this application.

  Returns:
    Boolean indicating whether or not the current user is an admin.
  """
    cookie_user_is_admin = users.is_current_user_admin()
    if cookie_user_is_admin:
        return cookie_user_is_admin

    # oauth.is_current_user_admin is not sufficient, we must first check that the
    # OAuth 2.0 user has a token minted for this application.
    rietveld_user = get_current_rietveld_oauth_user()
    if rietveld_user is None:
        return False

    return oauth.is_current_user_admin(EMAIL_SCOPE)
