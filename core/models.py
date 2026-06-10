from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base: every concrete model gets created_at / updated_at."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
