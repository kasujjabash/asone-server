"""Adding somebody, and them proving the address is theirs.

The flow AsOne asked for, on 5 September 2026:

    a lead adds the person and the system generates a password
    the lead passes that password on themselves
    a confirmation code is emailed to the address on the account
    the person enters the code, signs in, and is made to replace the password

**The password and the code travel by different routes on purpose.** The
lead reads the password off the screen and sends it by WhatsApp; the code
goes to the mailbox. Holding both is what says "this is the right person, at
the right address". Emailing the password too would put both in one inbox and
make the code decoration — which is why `test_the_password_is_never_emailed`
exists.

`AnUnconfirmedAccountIsNotAWayIn` is the other one to read: an account whose
address was mistyped must not be usable by whoever holds the password.
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

from accounts.models import EmailVerification, User
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
        """Add somebody; return (their generated password, their code)."""
        created = self.add_user()
        return created.data["password"], code_from_email(mail.outbox[-1])

    def confirm(self, email, code):
        return self.client.post(
            reverse("accounts:verify-email"),
            {"email": email, "code": code},
            format="json",
        )


class AddingSomebody(VerificationSetup):
    def test_the_lead_is_shown_a_generated_password(self):
        response = self.add_user()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["password"])

    def test_a_confirmation_code_is_emailed(self):
        self.add_user()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["joan@asone.test"])

    def test_the_password_is_never_emailed(self):
        """The rule the whole design rests on. If the password were in the
        inbox too, the code would prove nothing."""
        password, _ = self.added()

        self.assertNotIn(password, mail.outbox[-1].body)

    def test_they_must_replace_the_password(self):
        """Two people know it until they do."""
        self.add_user()

        self.assertTrue(User.objects.get(email="joan@asone.test").must_change_password)

    def test_the_address_starts_unconfirmed(self):
        self.add_user()

        self.assertFalse(User.objects.get(email="joan@asone.test").email_is_verified)

    def test_a_lead_may_type_a_password_instead(self):
        response = self.add_user(password="a-lead-chosen-passphrase")

        self.assertEqual(response.data["password"], "a-lead-chosen-passphrase")
        self.assertEqual(len(mail.outbox), 1)

    def test_the_code_is_not_stored_in_plain_text(self):
        _, code = self.added()

        self.assertNotIn(code, EmailVerification.objects.get().code_hash)


class AnUnconfirmedAccountIsNotAWayIn(VerificationSetup):
    """A mistyped address must not become a working account for whoever
    holds the password."""

    def test_signing_in_before_confirming_is_refused(self):
        password, _ = self.added()

        response = self.client.post(
            reverse("accounts:login"),
            {"email": "joan@asone.test", "password": password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("not been confirmed", str(response.data).lower())

    def test_no_sign_in_code_is_emailed_either(self):
        password, _ = self.added()
        mail.outbox.clear()

        self.client.post(
            reverse("accounts:login"),
            {"email": "joan@asone.test", "password": password},
            format="json",
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_the_refusal_comes_after_the_password_check(self):
        """A wrong password on an unconfirmed account is still 401 — telling
        somebody the account exists and is unconfirmed before they prove the
        password would give away more than the closed door already does."""
        self.added()

        response = self.client.post(
            reverse("accounts:login"),
            {"email": "joan@asone.test", "password": "not-the-password"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ConfirmingTheAddress(VerificationSetup):
    def test_the_right_code_confirms_it(self):
        _, code = self.added()

        response = self.confirm("joan@asone.test", code)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(User.objects.get(email="joan@asone.test").email_is_verified)

    def test_a_wrong_code_is_refused(self):
        self.added()

        self.assertEqual(
            self.confirm("joan@asone.test", "000000").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_they_can_then_sign_in_with_the_password_they_were_given(self):
        password, code = self.added()
        self.confirm("joan@asone.test", code)
        mail.outbox.clear()

        signed_in = sign_in(self.client, "joan@asone.test", password)

        self.assertEqual(signed_in.status_code, status.HTTP_200_OK)
        self.assertIn("access", signed_in.data)

    def test_and_are_then_made_to_replace_it(self):
        password, code = self.added()
        self.confirm("joan@asone.test", code)
        mail.outbox.clear()

        signed_in = sign_in(self.client, "joan@asone.test", password)

        self.assertTrue(signed_in.data["user"]["must_change_password"])

    def test_a_code_cannot_be_used_twice(self):
        _, code = self.added()
        self.confirm("joan@asone.test", code)

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_an_expired_code_is_refused(self):
        _, code = self.added()
        verification = EmailVerification.objects.get()
        verification.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        verification.save(update_fields=["expires_at"])

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_guessing_runs_out_of_tries(self):
        _, code = self.added()

        for _ in range(5):
            self.confirm("joan@asone.test", "000000")

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_a_deactivated_account_cannot_be_confirmed(self):
        _, code = self.added()
        user = User.objects.get(email="joan@asone.test")
        user.is_active = False
        user.save(update_fields=["is_active"])

        self.assertEqual(
            self.confirm("joan@asone.test", code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )


class WhenTheMailServerIsDown(VerificationSetup):
    """Found by probing on 5 September 2026, not by a test failing.

    Without a transaction around the two steps, a mail outage left an
    account nobody could confirm and nobody could recreate: the lead saw an
    error, retried, and was told the address already existed. Only a
    developer could clear it. Mail servers are exactly what is
    misconfigured on a first deploy.
    """

    def test_nothing_is_saved_when_the_code_cannot_be_sent(self):
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


class ResendingTheCode(VerificationSetup):
    """Seven days is not always long enough, and mail goes astray."""

    def url(self, user):
        return reverse("accounts:user-resend-verification", args=[user.pk])

    def test_a_lead_can_send_a_fresh_code(self):
        self.added()
        user = User.objects.get(email="joan@asone.test")
        mail.outbox.clear()

        self.client.force_authenticate(self.lead)
        response = self.client.post(self.url(user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_new_code_works_and_the_old_one_does_not(self):
        _, old_code = self.added()
        user = User.objects.get(email="joan@asone.test")

        self.client.force_authenticate(self.lead)
        self.client.post(self.url(user))
        self.client.force_authenticate(None)
        new_code = code_from_email(mail.outbox[-1])

        self.assertEqual(
            self.confirm("joan@asone.test", old_code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.confirm("joan@asone.test", new_code).status_code, status.HTTP_200_OK
        )

    def test_it_is_refused_once_the_address_is_confirmed(self):
        _, code = self.added()
        self.confirm("joan@asone.test", code)
        user = User.objects.get(email="joan@asone.test")

        self.client.force_authenticate(self.lead)

        self.assertEqual(
            self.client.post(self.url(user)).status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_warehouse_staff_cannot_resend(self):
        self.added()
        user = User.objects.get(email="joan@asone.test")
        clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.client.force_authenticate(clerk)

        self.assertEqual(
            self.client.post(self.url(user)).status_code, status.HTTP_403_FORBIDDEN
        )
