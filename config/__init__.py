"""Django project package; exposes the Celery app for workers."""

from config.celery import app as celery_app

__all__ = ("celery_app",)
