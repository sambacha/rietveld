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

import md5

from django.contrib.syndication.views import Feed
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.utils.feedgenerator import Atom1Feed

from codereview import library
from codereview import models


class BaseFeed(Feed):
    title = "Code Review"
    description = "Rietveld: Code Review Tool hosted on Google App Engine"
    feed_type = Atom1Feed

    def link(self):
        return reverse("codereview.views.index")

    def author_name(self):
        return "rietveld"

    def item_guid(self, item):
        return "urn:md5:%s" % (md5.new(str(item.key)).hexdigest())

    def item_link(self, item):
        if isinstance(item, models.PatchSet):
            if item.data is not None:
                return reverse(
                    "codereview.views.download",
                    args=[item.issue_key.id(), item.key.id()],
                )
            else:
                # Patch set is too large, only the splitted diffs are available.
                return reverse("codereview.views.show", args=[item.key.parent().id()])
        if isinstance(item, models.Message):
            return "%s#msg-%s" % (
                reverse("codereview.views.show", args=[item.issue_key.id()]),
                item.key.id(),
            )
        return reverse("codereview.views.show", args=[item.key.id()])

    def item_title(self, item):
        return "the title"

    def item_author_name(self, item):
        if isinstance(item, models.Issue):
            return library.get_nickname(item.owner, True)
        if isinstance(item, models.PatchSet):
            return library.get_nickname(item.issue_key.get().owner, True)
        if isinstance(item, models.Message):
            return library.get_nickname(item.sender, True)
        return "Rietveld"

    def item_pubdate(self, item):
        if isinstance(item, models.Issue):
            return item.modified
        if isinstance(item, models.PatchSet):
            # Use created, not modified, so that commenting on
            # a patch set does not bump its place in the RSS feed.
            return item.created
        if isinstance(item, models.Message):
            return item.date
        return None


class BaseUserFeed(BaseFeed):
    def get_object(self, request, *bits):
        """Returns the account for the requested user feed.

    bits is a list of URL path elements. The first element of this list
    should be the user's nickname. A 404 is raised if the list is empty or
    has more than one element or if the a user with that nickname
    doesn't exist.
    """
        if len(bits) != 1:
            raise ObjectDoesNotExist
        obj = bits[0]
        account = models.Account.get_account_for_nickname("%s" % obj)
        if account is None:
            raise ObjectDoesNotExist
        return account


class ReviewsFeed(BaseUserFeed):
    title = "Code Review - All issues I have to review"
    title_template = "feeds/reviews_title.html"
    description_template = "feeds/reviews_description.html"

    def items(self, obj):
        return _rss_helper(
            obj.email,
            models.Issue.closed == False,
            models.Issue.reviewers,
            use_email=True,
        )


class ClosedFeed(BaseUserFeed):
    title = "Code Review - Reviews closed by me"
    title_template = "feeds/closed_title.html"
    description_template = "feeds/closed_description.html"

    def items(self, obj):
        return _rss_helper(obj.email, models.Issue.closed == True, models.Issue.owner)


class MineFeed(BaseUserFeed):
    title = "Code Review - My issues"
    title_template = "feeds/mine_title.html"
    description_template = "feeds/mine_description.html"

    def items(self, obj):
        return _rss_helper(obj.email, models.Issue.closed == False, models.Issue.owner)


class AllFeed(BaseFeed):
    title = "Code Review - All issues"
    title_template = "feeds/all_title.html"
    description_template = "feeds/all_description.html"

    def items(self):
        query = models.Issue.query(
            models.Issue.closed == False, models.Issue.private == False
        ).order(-models.Issue.modified)
        return query.fetch(RSS_LIMIT)


class OneIssueFeed(BaseFeed):
    title_template = "feeds/issue_title.html"
    description_template = "feeds/issue_description.html"

    def link(self):
        return reverse("codereview.views.index")

    def get_object(self, request, *bits):
        if len(bits) != 1:
            raise ObjectDoesNotExist
        obj = models.Issue.get_by_id(int(bits[0]))
        if obj:
            return obj
        raise ObjectDoesNotExist

    def title(self, obj):
        return "Code review - Issue %d: %s" % (obj.key.id(), obj.subject)

    def items(self, obj):
        items = list(obj.patchsets) + list(obj.messages)
        items.sort(key=self.item_pubdate)
        return items


### RSS feeds ###

# Maximum number of issues reported by RSS feeds
RSS_LIMIT = 20


def _rss_helper(email, query_cond, query_attr, use_email=False):
    account = models.Account.get_account_for_email(email)
    if not account:
        return []

    attr_val = use_email and account.email or account.user
    query = models.Issue.query(
        query_cond, query_attr == attr_val, models.Issue.private == False
    ).order(-models.Issue.modified)
    return query.fetch(RSS_LIMIT)
