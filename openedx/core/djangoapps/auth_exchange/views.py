"""
Views to support exchange of authentication credentials.
The following are currently implemented:
    1. AccessTokenExchangeView:
       3rd party (social-auth) OAuth 2.0 access token -> 1st party (open-edx) OAuth 2.0 access token
    2. LoginWithAccessTokenView:
       1st party (open-edx) OAuth 2.0 access token -> session cookie
"""
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication, get_decoded_jwt_from_auth
import django.contrib.auth as auth
import social_django.utils as social_utils
from django.conf import settings
from django.contrib.auth import login
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from oauth2_provider import models as dot_models
from oauth2_provider.views.base import TokenView as DOTAccessTokenView
from rest_framework import permissions
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.views import APIView

from openedx.core.djangoapps.auth_exchange.forms import AccessTokenExchangeForm
from openedx.core.djangoapps.oauth_dispatch import adapters
from openedx.core.djangoapps.oauth_dispatch.api import create_dot_access_token
from openedx.core.djangoapps.safe_sessions.middleware import mark_user_change_as_expected
from openedx.core.lib.api.authentication import BearerAuthenticationAllowInactiveUser


class AccessTokenExchangeBase(APIView):
    """
    View for token exchange from 3rd party OAuth access token to 1st party
    OAuth access token.

    Note: This base class was originally created to support multiple libraries,
        but we currently only support django-oauth-toolkit (DOT).
    """
    # No CSRF protection is required because the provided 3rd party OAuth access
    #  token is sufficient
    authentication_classes = []
    allowed_methods = ['POST']

    @method_decorator(social_utils.psa("social:complete"))
    def dispatch(self, *args, **kwargs):  # pylint: disable=arguments-differ
        return super().dispatch(*args, **kwargs)

    def post(self, request, _backend):
        """
        Handle POST requests to get a first-party access token.
        """
        form = AccessTokenExchangeForm(request=request, oauth2_adapter=self.oauth2_adapter, data=request.POST)  # lint-amnesty, pylint: disable=no-member
        if not form.is_valid():
            error_response = self.error_response(form.errors)  # pylint: disable=no-member
            return error_response

        user = form.cleaned_data["user"]
        scope = form.cleaned_data["scope"]
        client = form.cleaned_data["client"]
        return self.exchange_access_token(request, user, scope, client)

    def exchange_access_token(self, request, user, scope, client):
        """
        Exchange third party credentials for an edx access token, and return a
        serialized access token response.
        """
        edx_access_token = self.create_access_token(request, user, scope, client)
        return self.access_token_response(edx_access_token)  # lint-amnesty, pylint: disable=no-member

    def _get_invalid_request_response(self, description):
        return Response(status=400, data={
            'error': 'invalid_request',
            'error_description': description,
        })


class DOTAccessTokenExchangeView(AccessTokenExchangeBase, DOTAccessTokenView):
    """
    View for token exchange from 3rd party OAuth access token to 1st party
    OAuth access token.  Uses django-oauth-toolkit (DOT) to manage access
    tokens.
    """

    oauth2_adapter = adapters.DOTAdapter()

    def create_access_token(self, request, user, scopes, client):
        """
        Create and return a new access token.
        """
        return create_dot_access_token(request, user, client, scopes=scopes)

    def access_token_response(self, token):
        """
        Wrap an access token in an appropriate response
        """
        return Response(data=token)

    def error_response(self, form_errors, **kwargs):  # pylint: disable=arguments-differ
        """
        Return an error response consisting of the errors in the form
        """
        error_code = form_errors.get('error_code', 400)
        return Response(status=error_code, data=form_errors, **kwargs)


class LoginWithAccessTokenView(APIView):
    """
    View for exchanging an access token for session cookies
    """
    authentication_classes = (BearerAuthenticationAllowInactiveUser, JwtAuthentication)
    permission_classes = (permissions.IsAuthenticated,)

    @staticmethod
    def _get_path_of_arbitrary_backend_for_user(user):
        """
        Return the path to the first found authentication backend that recognizes the given user.
        """
        for backend_path in settings.AUTHENTICATION_BACKENDS:
            backend = auth.load_backend(backend_path)
            if backend.get_user(user.id):
                return backend_path

    @staticmethod
    def _is_grant_password(access_token):
        """
        Check if the access token provided is DOT based and has password type grant.
        """
        token_query = dot_models.AccessToken.objects.select_related('user')
        dot_token = token_query.filter(token=access_token).first()
        if dot_token and dot_token.application.authorization_grant_type == dot_models.Application.GRANT_PASSWORD:
            return True

        return False

    @method_decorator(csrf_exempt)
    def post(self, request):
        """
        Handler for the POST method to this view.
        """
        # The django login method stores the user's id in request.session[SESSION_KEY] and the
        # path to the user's authentication backend in request.session[BACKEND_SESSION_KEY].
        # The login method assumes the backend path had been previously stored in request.user.backend
        # in the 'authenticate' call.  However, not all authentication providers do so.
        # So we explicitly populate the request.user.backend field here.

        if not hasattr(request.user, 'backend'):
            request.user.backend = self._get_path_of_arbitrary_backend_for_user(request.user)

        if isinstance(request.successful_authenticator, JwtAuthentication):
            jwt_payload = get_decoded_jwt_from_auth(request)
            is_grant_password = jwt_payload['grant_type'] == dot_models.Application.GRANT_PASSWORD
        else:
            # If not jwt then it must be bearer token
            is_grant_password = self._is_grant_password(request.auth)

        if not is_grant_password:
            raise AuthenticationFailed({
                'error_code': 'non_supported_token',
                'developer_message': 'Only support DOT type access token with grant type password. '
            })

        login(request, request.user)  # login generates and stores the user's cookies in the session
        response = HttpResponse(status=204)  # cookies stored in the session are returned with the response
        mark_user_change_as_expected(request.user.id)
        return response
