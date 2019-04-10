import pyotp
import uuid

from django.core.exceptions import ObjectDoesNotExist
from django.contrib import messages
from django.views.generic.base import TemplateResponseMixin, TemplateView, View
from django.shortcuts import redirect
from django.core.mail import send_mail
from django.http import (
    Http404,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
)
from django.contrib.sites.shortcuts import get_current_site

from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from allauth.account import app_settings
from allauth.account.models import EmailAddress
# from allauth.account.views import ConfirmEmailView
from allauth.account.adapter import get_adapter
from allauth.account.utils import (
    complete_signup,
    get_login_redirect_url,
    get_next_redirect_url,
    logout_on_password_change,
    passthrough_next_redirect_url,
    perform_login,
    sync_user_email_addresses,
    url_str_to_user_pk,
)
from allauth.account.models import EmailAddress, EmailConfirmation, EmailConfirmationHMAC

from lastwill.contracts.models import Contract
from lastwill.profile.helpers import valid_totp
from lastwill.settings import TRON_URL, MY_WISH_URL, SUPPORT_EMAIL, DEFAULT_FROM_EMAIL
from lastwill.profile.models import SubSite, UserSiteBalance, APIToken
from tron_wif.hex2wif import hex2tronwif


class ConfirmEmailView(TemplateResponseMixin, View):

    template_name = "account/email_confirm." + app_settings.TEMPLATE_EXTENSION

    def get(self, *args, **kwargs):
        print('get start', flush=True)
        try:
            self.object = self.get_object()
            print('get, object', self.object, flush=True)
            if app_settings.CONFIRM_EMAIL_ON_GET:
                print('get confirm email', app_settings.CONFIRM_EMAIL_ON_GET, flush=True)
                return self.post(*args, **kwargs)
        except Http404:
            self.object = None
        ctx = self.get_context_data()
        print('get ctx', ctx, flush=True)
        return self.render_to_response(ctx)

    def post(self, *args, **kwargs):
        print('post start', flush=True)
        self.object = confirmation = self.get_object()
        print('post confirmation', confirmation, flush=True)
        confirmation.confirm(self.request)
        get_adapter(self.request).add_message(
            self.request,
            messages.SUCCESS,
            'account/messages/email_confirmed.txt',
            {'email': confirmation.email_address.email})
        if app_settings.LOGIN_ON_EMAIL_CONFIRMATION:
            print('post login on email', app_settings.LOGIN_ON_EMAIL_CONFIRMATION, flush=True)
            resp = self.login_on_confirm(confirmation)
            if resp is not None:
                return resp

        redirect_url = self.get_redirect_url()
        print('post redirect url', redirect_url, flush=True)
        if not redirect_url:
            ctx = self.get_context_data()
            return self.render_to_response(ctx)
        return redirect(redirect_url)

    def login_on_confirm(self, confirmation):

        user_pk = None
        user_pk_str = get_adapter(self.request).unstash_user(self.request)
        if user_pk_str:
            user_pk = url_str_to_user_pk(user_pk_str)
        user = confirmation.email_address.user
        if user_pk == user.pk and self.request.user.is_anonymous:
            return perform_login(self.request,
                                 user,
                                 app_settings.EmailVerificationMethod.NONE,
                                 redirect_url=self.get_redirect_url)

        return None

    def get_object(self, queryset=None):
        print('get object start', flush=True)
        key = self.kwargs['key']
        print('get object key', self.kwargs['key'], flush=True)
        emailconfirmation = EmailConfirmationHMAC.from_key(key)
        print('get object emailconfirmation', emailconfirmation, flush=True)
        if not emailconfirmation:
            if queryset is None:
                queryset = self.get_queryset()
            try:
                emailconfirmation = queryset.get(key=key.lower())
            except EmailConfirmation.DoesNotExist:
                raise Http404()
        return emailconfirmation

    def get_queryset(self):
        qs = EmailConfirmation.objects.all_valid()
        qs = qs.select_related("email_address__user")
        return qs

    def get_context_data(self, **kwargs):
        ctx = kwargs
        ctx["confirmation"] = self.object
        site = get_current_site(self.request)
        ctx.update({'site': site})
        return ctx

    def get_redirect_url(self):
        return get_adapter(self.request).get_email_confirmation_redirect_url(
            self.request)


confirm_email = ConfirmEmailView.as_view()


class UserConfirmEmailView(ConfirmEmailView):
    def post(self, *args, **kwargs):
        self.object = confirmation = self.get_object()
        print('confirmation', confirmation, flush=True)
        print('request', self.request, flush=True)
        print('object', self.object, flush=True)
        confirmation.confirm(self.request)

        '''get_adapter(self.request).add_message(
            self.request,
            messages.SUCCESS,
            'account/messages/email_confirmed.txt',
            {'email': confirmation.email_address.email}
        )'''

        if app_settings.LOGIN_ON_EMAIL_CONFIRMATION:
            resp = self.login_on_confirm(confirmation)
            if resp is not None:
                return resp
        return redirect('/')


@api_view()
def profile_view(request):
    if request.user.is_anonymous:
        print('anonymous', flush=True)
        raise PermissionDenied()
    site_name = request.META['HTTP_HOST']
    # print('site name is', site_name)
    if site_name.startswith('cn'):
        site_name = site_name[2:]
    if site_name.startswith('local'):
        print('cut local')
        site_name = site_name[5:]
    # if site_name == TRON_URL:
    #     site_name = MY_WISH_URL
    site = SubSite.objects.get(site_name=site_name)
    # print(request.user.id, flush=True)
    user_balance = UserSiteBalance.objects.get(subsite=site, user=request.user)
    print(site_name, request.user.id, flush=True)
    answer = {
            'username': request.user.email if request.user.email else '{} {}'.format(request.user.first_name, request.user.last_name),
            'contracts': Contract.objects.filter(user=request.user).count(),
            'balance': str(user_balance.balance),
            'internal_address': user_balance.eth_address,
            'internal_btc_address': user_balance.btc_address,
            'use_totp': request.user.profile.use_totp,
            'is_social': request.user.profile.is_social,
            'id': request.user.id,
            'lang': request.user.profile.lang,
            'memo': user_balance.memo,
            'eos_address': 'mywishcoming',
            'tron_address': hex2tronwif(user_balance.tron_address) if user_balance.tron_address else ''
    }
    return Response(answer)


@api_view(http_method_names=['POST'])
def generate_key(request):
    user = request.user
    if user.is_anonymous or user.profile.use_totp:
        raise PermissionDenied()
    user.profile.totp_key = pyotp.random_base32()
    user.profile.save()
    return Response({
            'secret': user.profile.totp_key,
            'issuer': 'mywish.io',
            'user': user.email if user.email else str(user.id),
    })


@api_view(http_method_names=['POST'])
def enable_2fa(request):
    user = request.user
    if user.is_anonymous or not user.profile.totp_key:
        raise PermissionDenied()
    if not valid_totp(user, request.data['totp']):
        raise PermissionDenied()
    user.profile.use_totp = True
    user.profile.save()
    return Response({"result": "ok"})


@api_view(http_method_names=['POST'])
def disable_2fa(request):
    user = request.user
    if user.is_anonymous:
        raise PermissionDenied()
    if not user.profile.use_totp or not valid_totp(user, request.data['totp']):
        raise PermissionDenied()
    user.profile.use_totp = False
    user.profile.save()
    return Response({"result": "ok"})


@api_view(http_method_names=['POST'])
def resend_email(request):
    try:
        em = EmailAddress.objects.get(email=request.data['email'])
    except ObjectDoesNotExist:
        raise PermissionDenied(1)
    if em.verified:
        raise PermissionDenied(2)
    em.send_confirmation(request=request)
    return Response({"result": "ok"})


@api_view(http_method_names=['POST'])
def set_lang(request):
    user = request.user
    if user.is_anonymous:
        raise PermissionDenied()
    user.profile.lang = request.data['lang']
    user.profile.save()
    return Response({"result": "ok"})


@api_view(http_method_names=['POST'])
def create_api_token(request):
    user = request.user
    if user.is_anonymous:
        raise PermissionDenied()
    token_str = str(uuid.uuid4())
    text = request.data['label']
    api_token = APIToken(user=user, token=token_str, comment=text)
    api_token.save()
    send_mail(
        'User create api token',
        'User with id={id} {email_info} create token for api'.format(
            id=user.id, email_info='email is {email}'.format(email=user.email)
        ),
        DEFAULT_FROM_EMAIL,
        [SUPPORT_EMAIL]
    )
    answer = {
        "user_id": user.id,
        "token": api_token.token,
        "label": api_token.comment,
        "active": api_token.active,
        "last_accessed": api_token.last_accessed
    }
    return Response(answer)


@api_view(http_method_names=['GET'])
def get_api_tokens(request):
    user = request.user
    if user.is_anonymous:
        raise PermissionDenied()
    answer = {"tokens":[]}
    tokens = APIToken.objects.filter(user=user, active=True)
    for token in tokens:
        answer["tokens"].append(
            {
                "user_id": user.id,
                "token": token.token,
                "label": token.comment,
                "active": token.active,
                "last_accessed": token.last_accessed
            }
        )
    return Response(answer)


@api_view(http_method_names=['DELETE'])
def delete_api_token(request):
    user = request.user
    if user.is_anonymous:
        raise PermissionDenied()
    token_str = request.data['token']
    api_token = APIToken.objects.get(user=user, token=token_str)
    api_token.active = False
    api_token.save()
    return Response({"result": "ok"})


@api_view(http_method_names=['DELETE'])
def delete_api_tokens(request):
    user = request.user
    if user.is_anonymous:
        raise PermissionDenied()
    api_tokens = APIToken.objects.filter(user=user)
    for token in api_tokens:
        token.active = False
        token.save()
    return Response({"result": "ok"})
