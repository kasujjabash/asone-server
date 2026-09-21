"""Adding somebody, and them proving the address is theirs.

The flow AsOne asked for on 5 September 2026, as corrected on 21 September:

    a lead adds the person and the system generates a password
    the lead passes that password on themselves
    the person signs in with it
    a code is emailed to the address on the account
    they enter it — which confirms the address — and are then made to
    choose a password of their own

**The confirmation happens inside the first sign-in, not before it.** It used
to come first: a code was emailed at creation and sign-in was refused until
somebody typed it. That turned the first sign-in into a dead end — the person
had the password their lead had just handed them, typed it, and was told
their address "has not been confirmed yet" by a screen with nothing on it to
do next. `TheFirstSignInConfirmsTheAddress` is the class that pins the
replacement.

**The password and the code still travel by different routes.** The lead
reads the password off the screen and sends it by WhatsApp; the code goes to
the mailbox. Emailing the password too would put both in one inbox and make
the code decoration — which is why `test_the_password_is_never_emailed`
exists.

**A mistyped address still cannot become a working account.** That was the
job the old refusal was doing, and it survives without it: no token is issued
until a code emailed to that address comes back. See
`AMistypedAddressIsStillNotAWayIn`.

`ConfirmingWithoutSigningIn` covers what is left of the standalone code — no
longer the default path, but a lead can still push one.
"""

import re
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import EmailVerification, LoginChallenge, User
from accounts.tests.factories import build_sites, make_user, sign_in

Role = User.Role


def code_from_email(message):
    return re.search(r"\b(\d{6})\b", message.body).group(1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class VerificationSetup(APITestCase):
    def setUp(self):
        cache.clear()
        self.sites = build_sites()
        self.lead = make_user("sharon", Role.PROGRAM_LEAD, first_name="Sharon")
        mail.outbox.clear()

    def add_user(self, **overrides):
        self.client.force_authenticate(self.lead)
        payload = {
            "first_name": "Joan",
            "last_name": "Akello",
            "email": "joan@asone.test",
            "role": Role.WAREHOUSE_STAFF,
            "warehouse": self.sites["namayemba"].pk,
        }
        payload.update(overrides)
        response = self.client.post(
            reverse("accounts:user-list"), payload, format="json"
        )
        self.client.force_authenticate(None)
        return response

    def added(self):
        """Add somebody; return the password the lead was shown."""
        return self.add_user().data["password"]

    def password_step(self, password):
        """Step one of signing in. Returns the response."""
        return self.client.post(
            reverse("accounts:login"),
            {"email": "joan@asone.test", "password": password},
            format="json",
        )

    def confirm(self, email, code):
        return self.client.post(
            reverse("accounts:verify-email"),
            {"email": email, "code": code},
            format="json",
        )

    def joan(self):
        return User.objects.get(email="joan@asone.test")


class AddingSomebody(VerificationSetup):
    def test_the_lead_is_shown_a_generated_password(self):
        response = self.add_user()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["password"])

    def test_the_person_is_emailed_that_the_account_exists(self):
        self.add_user()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["joan@asone.test"])

    def test_no_code_is_emailed_at_this_point(self):
        """The code belongs to the sign-in, not to the creation.

        Two codes with two different lifetimes in front of somebody on their
        first day is what the old flow did, and the seven-day one was
        routinely the one they still had when they tried to sign in.
        """
        self.add_user()

        self.assertIsNone(re.search(r"\b\d{6}\b", mail.outbox[0].body))
        self.assertFalse(EmailVerification.objects.exists())

    def test_the_password_is_never_emailed(self):
        """The rule the whole design rests on. If the password were in the
        inbox too, the code would prove nothing."""
        password = self.added()

        self.assertNotIn(password, mail.outbox[-1].body)

    def test_they_must_replace_the_password(self):
        """Two people know it until they do."""
        self.add_user()

        self.assertTrue(self.joan().must_change_password)

    def test_the_address_starts_unconfirmed(self):
        self.add_user()

        self.assertFalse(self.joan().email_is_verified)

    def test_a_lead_may_type_a_password_instead(self):
        response = self.add_user(password="a-lead-chosen-passphrase")

        self.assertEqual(response.data["password"], "a-lead-chosen-passphrase")
        self.assertEqual(len(mail.outbox), 1)


class TheFirstSignInConfirmsTheAddress(VerificationSetup):
    """The dead end this replaced: the person had the password their lead
    had handed them, typed it, and was refused for a code sent days earlier
    to an inbox nobody had told them to check."""

    def test_the_password_is_accepted_though_the_address_is_unconfirmed(self):
        password = self.added()

        response = self.password_step(password)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("challenge", response.data)

    def test_a_code_is_emailed_at_that_point(self):
        password = self.added()
        mail.outbox.clear()

        self.password_step(password)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["joan@asone.test"])

    def test_the_first_code_says_what_it_is_for(self):
        """Somebody just handed a password by their lead is not expecting a
        security step, and an unexplained six-digit email reads as spam."""
        password = self.added()
        mail.outbox.clear()

        self.password_step(password)

        self.assertIn("confirm", mail.outbox[0].subject.lower())

    def test_the_screen_is_told_this_one_is_a_confirmation(self):
        """So the frontend can label it. An unexplained "enter your code"
        after a password somebody was handed by hand reads as a fault."""
        password = self.added()

        response = self.password_step(password)

        self.assertTrue(response.data["confirming_email"])
        self.assertIn("confirm your address", response.data["detail"].lower())

    def test_an_ordinary_sign_in_is_not_labelled_that_way(self):
        password = self.added()
        sign_in(self.client, "joan@asone.test", password)

        response = self.password_step(password)

        self.assertFalse(response.data["confirming_email"])
        self.assertIn("sign-in code", response.data["detail"].lower())

    def test_entering_it_signs_them_in(self):
        password = self.added()
        mail.outbox.clear()

        signed_in = sign_in(self.client, "joan@asone.test", password)

        self.assertEqual(signed_in.status_code, status.HTTP_200_OK)
        self.assertIn("access", signed_in.data)

    def test_entering_it_confirms_the_address(self):
        password = self.added()
        mail.outbox.clear()

        sign_in(self.client, "joan@asone.test", password)

        self.assertTrue(self.joan().email_is_verified)

    def test_and_they_are_then_made_to_choose_their_own_password(self):
        password = self.added()
        mail.outbox.clear()

        signed_in = sign_in(self.client, "joan@asone.test", password)

        self.assertTrue(signed_in.data["user"]["must_change_password"])

    def test_the_second_sign_in_is_an_ordinary_one(self):
        """Confirmation is a one-off. The code still comes every time — it is
        the second factor — but it stops calling itself a confirmation."""
        password = self.added()
        sign_in(self.client, "joan@asone.test", password)
        mail.outbox.clear()

        sign_in(self.client, "joan@asone.test", password)

        self.assertIn("sign-in code", mail.outbox[0].subject.lower())

    def test_a_wrong_code_leaves_the_address_unconfirmed(self):
        password = self.added()
        challenge = self.password_step(password).data["challenge"]

        self.client.post(
            reverse("accounts:login-verify"),
            {"challenge": challenge, "code": "000000"},
            format="json",
        )

        self.assertFalse(self.joan().email_is_verified)


class AMistypedAddressIsStillNotAWayIn(VerificationSetup):
    """The job the old refusal was doing, done by the code instead.

    Nothing here relies on sign-in being blocked up front. It relies on the
    only route to a token running through a message sent to the address on
    the account.
    """

    def test_the_password_alone_yields_no_token(self):
        password = self.added()

        response = self.password_step(password)

        self.assertNotIn("access", response.data)
        self.assertNotIn("refresh", response.data)

    def test_whoever_holds_the_password_cannot_finish_without_the_mailbox(self):
        password = self.added()
        challenge = self.password_step(password).data["challenge"]

        # Every guess someone who never saw the email could make.
        for guess in ("000000", "123456", "111111", "999999", "424242"):
            response = self.client.post(
                reverse("accounts:login-verify"),
                {"challenge": challenge, "code": guess},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(self.joan().email_is_verified)

    def test_an_address_that_bounces_never_becomes_an_account(self):
        """Caught while the lead is still looking at the form they mistyped,
        rather than a fortnight later when somebody cannot sign in."""
        with mock.patch(
            "accounts.services.send_mail", side_effect=OSError("no such mailbox")
        ):
            response = self.add_user(email="typo@asone.test")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(User.objects.filter(email="typo@asone.test").exists())

    def test_the_refusal_for_a_wrong_password_is_unchanged(self):
        """Still 401, and still says nothing about the address."""
        self.added()

        response = self.password_step("not-the-password")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("confirm", str(response.data).lower())


class ConfirmingWithoutSigningIn(VerificationSetup):
    """The standalone code. No longer the default path — a lead pushes one
    for somebody whose sign-in code will not reach them."""

    def resend_url(self, user):
        return reverse("accounts:user-resend-verification", args=[user.pk])

    def pushed(self):
        """A lead sends a standalone code. Returns it."""
        user = self.joan()
        mail.outbox.clear()
        self.client.force_authenticate(self.lead)
        self.client.post(self.resend_url(user))
        self.client.force_authenticate(None)
        return code_from_email(mail.outbox[-1])

    def test_a_lead_can_send_one(self):
        self.added()
        mail.outbox.clear()

        self.client.force_authenticate(self.lead)
        response = self.client.post(self.resend_url(self.joan()))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_right_code_confirms_it(self):
        self.added()
        code = self.pushed()

        response = self.confirm("joan@asone.test", code)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self.joan().email_is_verified)

    def test_the_code_is_not_stored_in_plain_text(self):
        self.added()
        code = self.pushed()

        self.assertNotIn(code, EmailVerification.objects.get().code_hash)

    def test_the_password_is_not_in_that_email_either(self):
        password = self.added()
        self.pushed()

        self.assertNotIn(password, mail.outbox[-1].body)

    def test_a_wrong_code_is_refused(self):
        self.added()
        self.pushed()

        self.assertEqual(
            self.confirm("joan@asone.test", "000000").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_a_code_cannot_be_used_twice(self):
        self.added()
        code = self.pushed()
        self.confirm("joan@asone.test", code)

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_an_expired_code_is_refused(self):
        self.added()
        code = self.pushed()
        verification = EmailVerification.objects.get()
        verification.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        verification.save(update_fields=["expires_at"])

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_guessing_runs_out_of_tries(self):
        self.added()
        code = self.pushed()

        for _ in range(5):
            self.confirm("joan@asone.test", "000000")

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_a_deactivated_account_cannot_be_confirmed(self):
        self.added()
        code = self.pushed()
        user = self.joan()
        user.is_active = False
        user.save(update_fields=["is_active"])

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_the_new_code_works_and_the_old_one_does_not(self):
        self.added()
        old_code = self.pushed()
        new_code = self.pushed()

        self.assertEqual(
            self.confirm("joan@asone.test", old_code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.confirm("joan@asone.test", new_code).status_code, status.HTTP_200_OK
        )

    def test_it_is_refused_once_the_address_is_confirmed(self):
        password = self.added()
        sign_in(self.client, "joan@asone.test", password)

        self.client.force_authenticate(self.lead)

        self.assertEqual(
            self.client.post(self.resend_url(self.joan())).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_warehouse_staff_cannot_send_one(self):
        self.added()
        clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.client.force_authenticate(clerk)

        self.assertEqual(
            self.client.post(self.resend_url(self.joan())).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class WhenTheMailServerIsDown(VerificationSetup):
    """Found by probing on 5 September 2026, not by a test failing.

    Without a transaction around the two steps, a mail outage left an
    account nobody could reach and nobody could recreate: the lead saw an
    error, retried, and was told the address already existed. Only a
    developer could clear it. Mail servers are exactly what is
    misconfigured on a first deploy.
    """

    def test_nothing_is_saved_when_the_email_cannot_be_sent(self):
        with mock.patch(
            "accounts.services.send_mail", side_effect=OSError("SMTP unreachable")
        ):
            self.add_user()

        self.assertFalse(User.objects.filter(email="joan@asone.test").exists())

    def test_the_lead_is_told_it_failed_rather_than_a_500(self):
        with mock.patch(
            "accounts.services.send_mail", side_effect=OSError("SMTP unreachable")
        ):
            response = self.add_user()

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_the_lead_can_simply_try_again(self):
        with mock.patch(
            "accounts.services.send_mail", side_effect=OSError("SMTP unreachable")
        ):
            self.add_user()

        self.assertEqual(self.add_user().status_code, status.HTTP_201_CREATED)

    def test_a_sign_in_whose_code_cannot_be_sent_does_not_confirm_anything(self):
        """`send_login_code` raising must not leave the address marked
        confirmed by a challenge nobody could ever have received."""
        password = self.added()

        with mock.patch(
            "accounts.services.send_mail", side_effect=OSError("SMTP unreachable")
        ):
            with self.assertRaises(OSError):
                self.password_step(password)

        self.assertFalse(self.joan().email_is_verified)
        self.assertFalse(
            LoginChallenge.objects.filter(consumed_at__isnull=True).exists()
        )
