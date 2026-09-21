"""The registration-request list — `/api/auth/registration-requests/`.

Written after the endpoint spent some time returning **login attempts**. A
stray copy of `LoginAttemptViewSet`'s four class attributes had been pasted
at the foot of `RegistrationRequestViewSet`'s body, after its `approve` and
`decline` actions. Python takes the last assignment in a class body, so those
silently replaced the correct `queryset`, `serializer_class` and
`filterset_fields` declared at the top — and nothing failed, because there
was no test for this endpoint at all.

The screen it feeds is the Users list, where pending requests appear as rows
a lead clicks to approve. It was showing sign-in audit rows instead: the same
address repeated, no name, and "Requested Invalid Date" where the serializer
had no `created_at` to give.

So these assert the shape rather than only the status code. A 200 was never
the thing that was wrong.
"""

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import LoginAttempt, RegistrationRequest, User
from accounts.tests.factories import make_user, sign_in

Role = User.Role


class RegistrationRequestListTests(APITestCase):
    def setUp(self):
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)

        self.pending = RegistrationRequest.objects.create(
            first_name="Robert",
            last_name="Mugisha",
            email="robert.m@asone.test",
            phone_number="+256 701 234567",
        )

        # A login attempt for the same person. The bug returned rows like
        # this one from the registration endpoint, so its presence is the
        # point of the fixture.
        LoginAttempt.objects.create(email="robert.m@asone.test", succeeded=True)

        self.client.force_authenticate(self.lead)
        self.url = reverse("accounts:registration-request-list")

    def test_returns_registration_requests_not_login_attempts(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        rows = response.data["results"]
        self.assertEqual(len(rows), 1)

        row = rows[0]
        # Fields only a RegistrationRequest has.
        self.assertEqual(row["first_name"], "Robert")
        self.assertEqual(row["last_name"], "Mugisha")
        self.assertEqual(row["email"], "robert.m@asone.test")
        self.assertIn("created_at", row)
        self.assertIn("status", row)

        # Fields only a LoginAttempt has. Their presence is the bug.
        self.assertNotIn("succeeded", row)
        self.assertNotIn("ip_address", row)
        self.assertNotIn("user_agent", row)

    def test_filters_by_status(self):
        """`?status=` is the filter the screen uses.

        LoginAttempt has no such field, so under the bug this was silently
        ignored and every row came back whatever was asked for.
        """
        RegistrationRequest.objects.create(
            first_name="Amina",
            last_name="Namubiru",
            email="amina.n@asone.test",
            status=RegistrationRequest.Status.DECLINED,
        )

        response = self.client.get(self.url, {"status": "PENDING"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["email"] for row in response.data["results"]],
            ["robert.m@asone.test"],
        )

    def test_only_the_leads_may_read_it(self):
        """Approving a request is creating a user, so it is the same audience
        as `UserViewSet` — see CanUpdateTables."""
        for role in (Role.FINANCE, Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF):
            with self.subTest(role=role):
                self.client.force_authenticate(make_user(f"user{role}", role))
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ApprovalProducesAnAccountThatCanSignIn(APITestCase):
    """Approving a request produces an account that can actually sign in.

    Three separate things used to stop that, and each was invisible until
    somebody tried it:

    **Approval was refused** unless the request carried a `verified_at`. Once
    the registration email code was removed nothing ever set one, so no
    request could be approved at all.

    **The account was created unconfirmed**, and sign-in refused an
    unconfirmed address, so the person a lead had just approved was told
    their address "has not been confirmed yet". Approval was made to stamp
    `email_verified_at` itself to get around that — marking an address
    proven that nobody had proved anything about.

    **Nobody was told.** Approval emailed nothing at all, so an approved
    registrant had an account and no word of it.

    Sign-in no longer refuses an unconfirmed address — the code it emails
    confirms it on the way through — so the stamp has gone with the thing it
    was working around, and approval now sends the same account-created
    email that adding somebody directly does.
    """

    def setUp(self):
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.request = RegistrationRequest.objects.create(
            first_name="Robert",
            last_name="Mugisha",
            email="robert.m@asone.test",
        )

    def test_a_request_can_be_approved_without_any_verification(self):
        from accounts import services

        self.assertFalse(self.request.is_email_verified)

        user, password = services.approve_registration(
            self.request, role=Role.FINANCE, decided_by=self.lead
        )

        self.assertEqual(user.email, "robert.m@asone.test")
        self.assertTrue(password)

    def test_the_address_is_not_marked_proven_by_the_approval_itself(self):
        """A lead approving a request has not checked the mailbox. The
        sign-in code does that, and only then is the field true."""
        from accounts import services

        user, _ = services.approve_registration(
            self.request, role=Role.FINANCE, decided_by=self.lead
        )

        user.refresh_from_db()
        self.assertIsNone(user.email_verified_at)

    def test_the_new_account_is_told_it_exists(self):
        from accounts import services

        mail.outbox.clear()
        services.approve_registration(
            self.request, role=Role.FINANCE, decided_by=self.lead
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["robert.m@asone.test"])

    def test_the_approved_person_can_sign_in(self):
        """End to end, which is the only way the three failures above were
        ever going to be caught."""
        from accounts import services

        user, password = services.approve_registration(
            self.request, role=Role.FINANCE, decided_by=self.lead
        )
        mail.outbox.clear()

        signed_in = sign_in(self.client, "robert.m@asone.test", password)

        self.assertEqual(signed_in.status_code, status.HTTP_200_OK)
        self.assertIn("access", signed_in.data)

        user.refresh_from_db()
        self.assertTrue(user.email_is_verified)
