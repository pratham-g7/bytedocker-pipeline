from functools import wraps

from django.http import HttpResponseForbidden


def role_required(*roles):
    """View decorator: 403 unless request.user.role is one of `roles`."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.user.role not in roles:
                return HttpResponseForbidden("Insufficient role")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def scope_to_user(queryset, user, field: str = "owner"):
    """Reps see only their own records; managers and admins see everything.

    Applied at the queryset level per UI_SPEC §5 — never in templates.
    """
    if user.role == "rep":
        return queryset.filter(**{field: user})
    return queryset
