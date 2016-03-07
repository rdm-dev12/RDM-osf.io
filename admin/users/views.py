from django.shortcuts import redirect
from django.views.defaults import page_not_found

from website.project.model import User

from admin.base.views import GuidFormView, GuidView
from admin.users.templatetags.user_extras import reverse_user
from .serializers import serialize_user


def remove_2_factor(request, guid):
    user = User.load(guid)
    try:
        user.delete_addon('twofactor')
    except AttributeError:
        page_not_found(request)
    return redirect(reverse_user(guid))


class UserFormView(GuidFormView):
    template_name = 'users/search.html'
    object_type = 'user'

    @property
    def success_url(self):
        return reverse_user(self.guid)


class UserView(GuidView):
    template_name = 'users/user.html'
    context_object_name = 'user'

    def get_object(self, queryset=None):
        self.guid = self.kwargs.get('guid', None)
        return serialize_user(User.load(self.guid))
