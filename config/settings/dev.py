from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DEBUG", default=True)
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
