"""Configuration."""

import logging
import os
import re
import sys

from google.appengine.ext.appstats import recording

logging.info("Loading %s from %s", __name__, __file__)

# Custom webapp middleware to add Appstats.
def webapp_add_wsgi_middleware(app):
    # If reenabling appstats, do not forget to reeanble it in app.yaml too.
    # app = recording.appstats_wsgi_middleware(app)
    return app


# Custom Appstats path normalization.
def appstats_normalize_path(path):
    if path.startswith("/user/"):
        return "/user/X"
    if path.startswith("/user_popup/"):
        return "/user_popup/X"
    if "/diff/" in path:
        return "/X/diff/..."
    if "/diff2/" in path:
        return "/X/diff2/..."
    if "/patch/" in path:
        return "/X/patch/..."
    if path.startswith("/rss/"):
        i = path.find("/", 5)
        if i > 0:
            return path[:i] + "/X"
    return re.sub(r"\d+", "X", path)


# Segregate Appstats by runtime (python vs. python27).
appstats_KEY_NAMESPACE = "__appstats_%s__" % os.getenv("APPENGINE_RUNTIME")

# Enable Interactive Playground.
appstats_SHELL_OK = True

# Enable RPC cost calculation.
appstats_CALC_RPC_COSTS = True

# Enable third-party imports
sys.path.append(os.path.join(os.path.dirname(__file__), "third_party"))
