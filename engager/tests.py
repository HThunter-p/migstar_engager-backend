from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Campaign, CampaignBudget, EngagerQuestion, PaymentRequest, PlatformSettings, SubscriptionLevel, Task, TaskClaim, User, WalletLedger, WithdrawalRequest


class SwitchRoleTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="role-switcher", password="test-password-123", roles=["ENGAGER"])
        self.client.force_authenticate(self.user)

    def test_registered_user_can_add_the_other_role_without_losing_their_current_role(self):
        response = self.client.post("/api/me/switch-role/", {"role": "CREATOR"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["roles"], ["ENGAGER", "CREATOR"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.roles, ["ENGAGER", "CREATOR"])

    def test_switching_to_an_existing_role_does_not_duplicate_it(self):
        response = self.client.post("/api/me/switch-role/", {"role": "ENGAGER"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["roles"], ["ENGAGER"])

    def test_unknown_role_is_rejected(self):
        response = self.client.post("/api/me/switch-role/", {"role": "ADMIN"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_role_switch_requires_authentication(self):
        self.client.force_authenticate(user=None)

        response = self.client.post("/api/me/switch-role/", {"role": "CREATOR"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class CampaignDiscoveryAccessTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="campaign-viewer", password="test-password")
        self.other_user = User.objects.create_user(username="other-campaign-owner", password="test-password")
        self.approved_campaign = Campaign.objects.create(
            creator=self.other_user,
            platform="YOUTUBE",
            objective="VIEWERS",
            url="https://www.youtube.com/watch?v=approved",
            status="APPROVED",
        )
        self.own_pending_campaign = Campaign.objects.create(
            creator=self.user,
            platform="YOUTUBE",
            objective="VIEWERS",
            url="https://www.youtube.com/watch?v=pending",
            status="PENDING",
        )
        Campaign.objects.create(
            creator=self.other_user,
            platform="YOUTUBE",
            objective="VIEWERS",
            url="https://www.youtube.com/watch?v=private",
            status="PENDING",
        )

    def test_campaign_list_requires_sign_in(self):
        response = self.client.get("/api/campaigns/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_signed_in_user_sees_approved_campaigns_and_their_own_submissions(self):
        self.client.force_authenticate(self.user)

        response = self.client.get("/api/campaigns/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        campaign_ids = {campaign["id"] for campaign in response.data}
        self.assertEqual(campaign_ids, {self.approved_campaign.pk, self.own_pending_campaign.pk})

    def test_creator_only_sees_their_own_campaigns(self):
        self.user.roles = ["CREATOR"]
        self.user.save(update_fields=["roles"])
        self.client.force_authenticate(self.user)

        response = self.client.get("/api/campaigns/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {campaign["id"] for campaign in response.data},
            {self.own_pending_campaign.pk},
        )

    def test_approved_campaigns_cannot_be_edited_through_the_public_campaign_endpoint(self):
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            f"/api/campaigns/{self.approved_campaign.pk}/",
            {"url": "https://www.youtube.com/watch?v=changed"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_expired_budget_campaigns_are_hidden_from_engagers(self):
        package = CampaignBudget.objects.create(
            platform="YOUTUBE",
            objective="VIEWERS",
            quantity=10,
            amount="25.00",
        )
        expired_campaign = Campaign.objects.create(
            creator=self.other_user,
            platform="YOUTUBE",
            objective="VIEWERS",
            url="https://www.youtube.com/watch?v=expired",
            status="APPROVED",
            budget_package=package,
            subscription_until=timezone.now() - timedelta(seconds=1),
        )
        self.client.force_authenticate(self.user)

        response = self.client.get("/api/campaigns/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(expired_campaign.pk, {campaign["id"] for campaign in response.data})


class TaskAnswerTimingTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="task-timer-user",
            password="StrongPass123!",
            roles=["ENGAGER"],
            engager_active=True,
        )
        campaign = Campaign.objects.create(
            creator=self.user,
            platform="YOUTUBE",
            objective="VIEWERS",
            url="https://www.youtube.com/watch?v=demo",
            status="APPROVED",
        )
        self.task = Task.objects.create(
            campaign=campaign,
            question="What is the answer?",
            seconds=60,
        )
        EngagerQuestion.objects.create(platform="YOUTUBE", question="What appeared in the video?")
        self.client.force_authenticate(self.user)

    def test_task_cannot_be_claimed_until_viewing_duration_has_elapsed(self):
        start = self.client.post(f"/api/tasks/{self.task.pk}/start/", {}, format="json")
        claim = self.client.post(f"/api/tasks/{self.task.pk}/claim/", {}, format="json")

        response = self.client.post(
            f"/api/tasks/{self.task.pk}/answer/",
            {"answer": "A valid answer"},
            format="json",
        )

        self.assertEqual(start.status_code, status.HTTP_201_CREATED)
        self.assertEqual(claim.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("watch the content", claim.data["detail"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("then claim the task", response.data["detail"])

    def test_task_can_be_claimed_and_answered_after_viewing_duration(self):
        self.client.post(f"/api/tasks/{self.task.pk}/start/", {}, format="json")
        TaskClaim.objects.filter(task=self.task, user=self.user).update(
            started_at=timezone.now() - timedelta(seconds=self.task.seconds + 1)
        )
        claim = self.client.post(f"/api/tasks/{self.task.pk}/claim/", {}, format="json")

        response = self.client.post(
            f"/api/tasks/{self.task.pk}/answer/",
            {"answer": "A valid answer"},
            format="json",
        )

        self.assertEqual(claim.status_code, status.HTTP_200_OK)
        self.assertEqual(claim.data["status"], "CLAIMED")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "SUBMITTED")


class WithdrawalBalanceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="withdrawal-test-user",
            password="StrongPass123!",
        )
        self.client.force_authenticate(self.user)
        self.payload = {"amount": "120.00", "mpesa_number": "254711222333"}

    def test_withdrawal_is_rejected_when_balance_is_insufficient(self):
        response = self.client.post("/api/withdrawals/", self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("available demo balance", response.data["amount"])

    def test_withdrawal_is_rejected_below_configured_minimum(self):
        WalletLedger.objects.create(
            user=self.user,
            entry_type="CREDIT",
            amount="200.00",
            description="Test funding",
        )
        response = self.client.post(
            "/api/withdrawals/",
            {**self.payload, "amount": "119.99"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Minimum withdrawal is KSh 120.00", response.data["amount"])

    def test_pending_withdrawals_reduce_available_balance(self):
        WalletLedger.objects.create(
            user=self.user,
            entry_type="CREDIT",
            amount="200.00",
            description="Test funding",
        )
        first_response = self.client.post("/api/withdrawals/", self.payload, format="json")
        second_response = self.client.post("/api/withdrawals/", self.payload, format="json")

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.user.withdrawals.count(), 1)


class PaymentRequestValidationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="payment-test-user",
            password="StrongPass123!",
            roles=["CREATOR", "ENGAGER"],
        )
        self.client.force_authenticate(self.user)
        self.level = SubscriptionLevel.objects.create(
            name="Payment test level",
            fee="450.00",
        )

    def test_me_endpoint_returns_only_the_users_subscribed_level(self):
        self.user.selected_level = self.level
        self.user.engager_active = True
        self.user.save(update_fields=["selected_level", "engager_active"])

        response = self.client.get("/api/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["selected_level_name"], self.level.name)
        self.assertEqual(response.data["selected_level_fee"], "450.00")

    def test_creator_activation_uses_configured_fee(self):
        response = self.client.post(
            "/api/payment-requests/",
            {"request_type": "CREATOR_ACTIVATION", "amount": "1.00"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("configured fee", str(response.data["amount"]))

    def test_valid_creator_activation_request_is_accepted_only_once_while_pending(self):
        payload = {
            "request_type": "CREATOR_ACTIVATION",
            "amount": str(PlatformSettings.current().creator_activation_fee),
        }
        first_response = self.client.post("/api/payment-requests/", payload, format="json")
        second_response = self.client.post("/api/payment-requests/", payload, format="json")

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pending request", str(second_response.data))

    def test_engager_level_request_uses_selected_level_fee(self):
        response = self.client.post(
            "/api/payment-requests/",
            {
                "request_type": "ENGAGER_LEVEL",
                "amount": "1.00",
                "level": self.level.pk,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("configured fee", str(response.data["amount"]))

    def test_engager_level_subscription_is_one_time_and_pending_requests_block_other_levels(self):
        another_level = SubscriptionLevel.objects.create(
            name="Another payment test level",
            fee="600.00",
        )
        first = self.client.post(
            "/api/payment-requests/",
            {"request_type": "ENGAGER_LEVEL", "amount": "450.00", "level": self.level.pk},
            format="json",
        )
        second = self.client.post(
            "/api/payment-requests/",
            {"request_type": "ENGAGER_LEVEL", "amount": "600.00", "level": another_level.pk},
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pending request", str(second.data))

    def test_engager_cannot_subscribe_to_a_level_again_after_activation(self):
        self.user.engager_active = True
        self.user.selected_level = self.level
        self.user.save(update_fields=["engager_active", "selected_level"])

        response = self.client.post(
            "/api/payment-requests/",
            {"request_type": "ENGAGER_LEVEL", "amount": "450.00", "level": self.level.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only be subscribed to once", str(response.data))


class AdminReviewDashboardTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin-review-user",
            email="admin-review@example.com",
            password="StrongPass123!",
            roles=["CREATOR"],
            is_staff=True,
        )
        self.creator = User.objects.create_user(
            username="creator-review-user",
            email="creator-review@example.com",
            password="StrongPass123!",
            roles=["CREATOR"],
        )
        self.engager = User.objects.create_user(
            username="engager-review-user",
            email="engager-review@example.com",
            password="StrongPass123!",
            roles=["ENGAGER"],
        )
        self.level = SubscriptionLevel.objects.create(
            name="Review level",
            fee="100.00",
            sample_daily_earning="25.00",
        )
        self.engager.selected_level = self.level
        self.engager.engager_active = True
        self.engager.save(update_fields=["selected_level", "engager_active"])
        self.campaign = Campaign.objects.create(
            creator=self.creator,
            platform="YOUTUBE",
            objective="VIEWERS",
            url="https://www.youtube.com/watch?v=reviewcamp",
            status="PENDING",
            budget="400.00",
        )
        self.payment = PaymentRequest.objects.create(
            user=self.creator,
            request_type="CREATOR_ACTIVATION",
            amount=PlatformSettings.current().creator_activation_fee,
            transaction_code="TX-001",
            evidence_note="Draft review",
            status="UNDER_REVIEW",
        )
        self.payment.evidence_screenshot.save(
            "admin-test-proof.png",
            SimpleUploadedFile("admin-test-proof.png", b"\x89PNG\r\n\x1a\nproof", content_type="image/png"),
            save=True,
        )
        self.payment_evidence_name = self.payment.evidence_screenshot.name
        self.withdrawal = WithdrawalRequest.objects.create(
            user=self.engager,
            amount="180.00",
            mpesa_number="254700123456",
        )
        self.task = Task.objects.create(
            campaign=self.campaign,
            question="Is this a test?",
            seconds=15,
        )
        self.claim = TaskClaim.objects.create(
            task=self.task,
            user=self.engager,
            answer="Yes",
            status="SUBMITTED",
        )
        self.client.force_authenticate(self.admin)

    def test_admin_can_fetch_review_queue(self):
        self.assertEqual(self.client.get("/api/admin/review/campaigns/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get("/api/admin/review/payments/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get("/api/admin/review/withdrawals/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get("/api/admin/review/task-answers/").status_code, status.HTTP_200_OK)

    def test_admin_can_decide_campaign_payment_withdrawal_and_task_statuses(self):
        campaign_response = self.client.post(
            f"/api/admin/review/campaigns/{self.campaign.id}/",
            {"status": "APPROVED", "admin_note": "Looks valid."},
            format="json",
        )
        with self.captureOnCommitCallbacks(execute=True):
            payment_response = self.client.post(
                f"/api/admin/review/payments/{self.payment.id}/",
                {"status": "APPROVED", "admin_note": "Approved."},
                format="json",
            )
        withdrawal_response = self.client.post(
            f"/api/admin/review/withdrawals/{self.withdrawal.id}/",
            {"status": "APPROVED", "admin_note": "Payout approved."},
            format="json",
        )
        task_response = self.client.post(
            f"/api/admin/review/task-answers/{self.claim.id}/",
            {"status": "APPROVED", "admin_note": "Correct answer."},
            format="json",
        )

        self.assertEqual(campaign_response.status_code, status.HTTP_200_OK)
        self.assertEqual(payment_response.status_code, status.HTTP_200_OK)
        self.assertEqual(withdrawal_response.status_code, status.HTTP_200_OK)
        self.assertEqual(task_response.status_code, status.HTTP_200_OK)

        self.campaign.refresh_from_db()
        self.payment.refresh_from_db()
        self.withdrawal.refresh_from_db()
        self.claim.refresh_from_db()

        self.assertEqual(self.campaign.status, "APPROVED")
        self.assertEqual(self.payment.status, "APPROVED")
        self.assertFalse(default_storage.exists(self.payment_evidence_name))
        self.assertEqual(self.withdrawal.status, "APPROVED")
        self.assertEqual(self.claim.status, "APPROVED")
        self.assertEqual(
            WalletLedger.objects.filter(user=self.engager, reference=f"task-claim-{self.claim.pk}").count(),
            1,
        )
        earning = WalletLedger.objects.get(user=self.engager, reference=f"task-claim-{self.claim.pk}")
        self.assertEqual(str(earning.amount), str(self.level.sample_daily_earning))
        repeated_approval = self.client.post(
            f"/api/admin/review/task-answers/{self.claim.id}/",
            {"status": "APPROVED"},
            format="json",
        )
        self.assertEqual(repeated_approval.status_code, status.HTTP_200_OK)
        self.assertEqual(
            WalletLedger.objects.filter(user=self.engager, reference=f"task-claim-{self.claim.pk}").count(),
            1,
        )

        self.client.force_authenticate(self.creator)
        campaigns_response = self.client.get("/api/campaigns/")
        self.assertEqual(campaigns_response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(
            campaign["id"] == self.campaign.id and campaign["status"] == "APPROVED"
            for campaign in campaigns_response.data
        ))

    def test_non_admin_users_are_blocked(self):
        self.client.force_authenticate(self.creator)

        response = self.client.get("/api/admin/review/campaigns/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_me_endpoint_exposes_staff_flag(self):
        response = self.client.get("/api/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_staff"]) 


class CampaignSubscriptionTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="subscription-admin",
            email="subscription-admin@example.com",
            password="secure-password",
            is_staff=True,
        )
        self.superuser = User.objects.create_superuser(
            username="subscription-superuser",
            email="subscription-superuser@example.com",
            password="secure-password",
        )
        self.creator = User.objects.create_user(
            username="subscription-creator",
            email="subscription-creator@example.com",
            password="secure-password",
            roles=["CREATOR"],
            creator_active=True,
        )
        self.engager = User.objects.create_user(
            username="subscription-engager",
            email="subscription-engager@example.com",
            password="secure-password",
            roles=["ENGAGER"],
            engager_active=True,
        )
        self.package = CampaignBudget.objects.create(
            platform="YOUTUBE",
            objective="FOLLOWERS",
            quantity=50,
            amount="100.00",
            validity_days=30,
        )
        self.campaign = Campaign.objects.create(
            creator=self.creator,
            platform="YOUTUBE",
            objective="FOLLOWERS",
            url="https://www.youtube.com/watch?v=subscription",
            budget=self.package.amount,
            budget_package=self.package,
            subscription_days=self.package.validity_days,
            status="APPROVED",
        )
        self.task = Task.objects.create(campaign=self.campaign)

    def test_campaign_packages_are_only_visible_to_authenticated_creators(self):
        self.client.force_authenticate(self.creator)
        creator_response = self.client.get("/api/campaign-budgets/")
        self.assertEqual(creator_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(creator_response.data), 1)

        self.client.force_authenticate(self.engager)
        engager_response = self.client.get("/api/campaign-budgets/")
        self.assertEqual(engager_response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=None)
        anonymous_response = self.client.get("/api/campaign-budgets/")
        self.assertEqual(anonymous_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_campaign_review_approval_does_not_activate_subscription(self):
        self.campaign.status = "PENDING"
        self.campaign.save(update_fields=["status"])
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            f"/api/admin/review/campaigns/{self.campaign.pk}/",
            {"status": "APPROVED"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, "APPROVED")
        self.assertEqual(self.campaign.approved_budget, 0)
        self.assertIsNone(self.campaign.subscription_until)

        self.client.force_authenticate(self.engager)
        self.assertEqual(self.client.get("/api/tasks/").data, [])
        self.assertNotIn(self.campaign.pk, {
            campaign["id"] for campaign in self.client.get("/api/campaigns/").data
        })

    def test_creator_gets_two_free_campaign_posts_then_later_posts_are_paid(self):
        creator = User.objects.create(
            username="free-trial-creator",
            email="free-trial-creator@example.com",
            roles=["CREATOR"],
            creator_active=True,
        )
        self.client.force_authenticate(creator)
        payload = {
            "platform": "YOUTUBE",
            "content_type": "CHANNEL",
            "objective": "FOLLOWERS",
            "budget_package": self.package.pk,
            "url": "https://www.youtube.com/watch?v=free-trial",
        }

        with patch("engager.views.safe_public_url", return_value=False):
            first = self.client.post("/api/campaigns/", payload, format="json")
            second = self.client.post("/api/campaigns/", payload, format="json")
            third = self.client.post("/api/campaigns/", payload, format="json")

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(third.status_code, status.HTTP_201_CREATED)
        self.assertTrue(first.data["is_free_trial"])
        self.assertTrue(second.data["is_free_trial"])
        self.assertFalse(third.data["is_free_trial"])
        self.assertEqual(self.client.get("/api/me/").data["free_campaigns_remaining"], 0)

    def test_approved_free_trial_is_activated_and_opens_its_paid_package_capacity(self):
        campaign = Campaign.objects.create(
            creator=self.creator,
            platform="YOUTUBE",
            objective="FOLLOWERS",
            url="https://www.youtube.com/watch?v=free-approved",
            budget=self.package.amount,
            budget_package=self.package,
            target_quantity=self.package.quantity,
            subscription_days=self.package.validity_days,
            is_free_trial=True,
        )
        task = Task.objects.create(campaign=campaign)
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            f"/api/admin/review/campaigns/{campaign.pk}/",
            {"status": "APPROVED"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "APPROVED")
        self.assertEqual(campaign.approved_budget, campaign.budget)
        self.assertGreater(campaign.subscription_until, timezone.now())

        self.client.force_authenticate(self.engager)
        self.assertEqual(len(self.client.get("/api/tasks/").data), 1)
        EngagerQuestion.objects.create(platform="YOUTUBE", question="What is shown on this channel?")
        start = self.client.post(f"/api/tasks/{task.pk}/start/", {}, format="json")
        self.assertEqual(start.status_code, status.HTTP_201_CREATED)
        TaskClaim.objects.filter(task=task, user=self.engager).update(
            started_at=timezone.now() - timedelta(seconds=task.seconds + 1)
        )
        claim = self.client.post(f"/api/tasks/{task.pk}/claim/", {}, format="json")
        self.assertEqual(claim.status_code, status.HTTP_200_OK)

    def test_admin_can_remove_an_approved_campaign_with_a_creator_notice(self):
        self.client.force_authenticate(self.admin)

        inventory = self.client.get("/api/admin/review/campaigns/")
        self.assertTrue(any(item["id"] == self.campaign.pk for item in inventory.data))

        missing_notice = self.client.post(
            f"/api/admin/review/campaigns/{self.campaign.pk}/",
            {"status": "REMOVED"},
            format="json",
        )
        self.assertEqual(missing_notice.status_code, status.HTTP_400_BAD_REQUEST)

        notice = "This campaign no longer meets platform guidelines."
        removed = self.client.post(
            f"/api/admin/review/campaigns/{self.campaign.pk}/",
            {"status": "REMOVED", "admin_note": notice},
            format="json",
        )
        self.assertEqual(removed.status_code, status.HTTP_200_OK)
        self.assertEqual(removed.data["status"], "REMOVED")

        self.client.force_authenticate(self.creator)
        creator_campaigns = self.client.get("/api/campaigns/")
        creator_campaign = next(item for item in creator_campaigns.data if item["id"] == self.campaign.pk)
        self.assertEqual(creator_campaign["status"], "REMOVED")
        self.assertEqual(creator_campaign["admin_note"], notice)

        self.client.force_authenticate(self.engager)
        engager_campaigns = self.client.get("/api/campaigns/")
        self.assertNotIn(self.campaign.pk, {item["id"] for item in engager_campaigns.data})
        self.assertEqual(self.client.get("/api/tasks/").data, [])

    def test_dual_role_creator_can_fund_campaign_with_engager_wallet(self):
        dual_creator = User.objects.create_user(
            username="dual-role-wallet-user",
            email="dual-role-wallet@example.com",
            password="safe-password",
            roles=["CREATOR", "ENGAGER"],
            creator_active=True,
            engager_active=True,
        )
        campaign = Campaign.objects.create(
            creator=dual_creator,
            platform="YOUTUBE",
            objective="FOLLOWERS",
            url="https://www.youtube.com/watch?v=walletfund",
            budget=self.package.amount,
            budget_package=self.package,
            subscription_days=self.package.validity_days,
            status="APPROVED",
        )
        WalletLedger.objects.create(
            user=dual_creator,
            entry_type="CREDIT",
            amount="150.00",
            description="Engager task earnings",
        )
        self.client.force_authenticate(dual_creator)

        response = self.client.post(
            "/api/wallet/fund-campaign/",
            {"campaign": campaign.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["demo_balance"], "50.00")
        self.assertEqual(
            WalletLedger.objects.filter(user=dual_creator, entry_type="DEBIT", amount="100.00").count(),
            1,
        )
        self.assertTrue(PaymentRequest.objects.filter(
            user=dual_creator,
            campaign=campaign,
            request_type="CAMPAIGN_BUDGET",
            status="APPROVED",
        ).exists())
        campaign.refresh_from_db()
        self.assertGreater(campaign.subscription_until, timezone.now())

        duplicate = self.client.post(
            "/api/wallet/fund-campaign/",
            {"campaign": campaign.pk},
            format="json",
        )
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)

    def test_wallet_funding_rejects_insufficient_funds_and_accounts_without_both_roles(self):
        dual_creator = User.objects.create_user(
            username="wallet-insufficient-user",
            email="wallet-insufficient@example.com",
            password="safe-password",
            roles=["CREATOR", "ENGAGER"],
            creator_active=True,
        )
        campaign = Campaign.objects.create(
            creator=dual_creator,
            platform="YOUTUBE",
            objective="FOLLOWERS",
            url="https://www.youtube.com/watch?v=walletshort",
            budget=self.package.amount,
            budget_package=self.package,
            subscription_days=self.package.validity_days,
            status="APPROVED",
        )
        self.client.force_authenticate(dual_creator)
        insufficient = self.client.post(
            "/api/wallet/fund-campaign/",
            {"campaign": campaign.pk},
            format="json",
        )
        self.assertEqual(insufficient.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Insufficient engager wallet funds", str(insufficient.data))
        self.assertFalse(WalletLedger.objects.filter(user=dual_creator, entry_type="DEBIT").exists())

        self.client.force_authenticate(self.creator)
        not_dual_role = self.client.post(
            "/api/wallet/fund-campaign/",
            {"campaign": self.campaign.pk},
            format="json",
        )
        self.assertEqual(not_dual_role.status_code, status.HTTP_403_FORBIDDEN)

    def test_campaign_submission_requires_an_admin_configured_package(self):
        self.client.force_authenticate(self.creator)

        response = self.client.post(
            "/api/campaigns/",
            {
                "platform": "YOUTUBE",
                "content_type": "CHANNEL",
                "objective": "FOLLOWERS",
                "url": "https://www.youtube.com/watch?v=new-campaign",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("budget_package", response.data)

    def test_video_submission_creates_a_task_with_detected_duration(self):
        self.client.force_authenticate(self.creator)
        youtube_dl = MagicMock()
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Task video",
            "thumbnail": "",
            "duration": 90,
        }
        with patch("engager.views.safe_public_url", return_value=True), patch.dict(
            "sys.modules", {"yt_dlp": SimpleNamespace(YoutubeDL=youtube_dl)}
        ):
            response = self.client.post(
                "/api/campaigns/",
                {
                    "platform": "YOUTUBE",
                    "content_type": "VIDEO",
                    "objective": "FOLLOWERS",
                    "budget_package": self.package.pk,
                    "url": "https://www.youtube.com/watch?v=task-video",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        campaign = Campaign.objects.get(pk=response.data["id"])
        task = Task.objects.get(campaign=campaign)
        self.assertEqual(campaign.content_type, "VIDEO")
        self.assertEqual(task.seconds, 90)

    def test_video_submission_uses_creator_duration_when_metadata_has_none(self):
        self.client.force_authenticate(self.creator)
        instagram_package = CampaignBudget.objects.create(
            platform="INSTAGRAM",
            objective="FOLLOWERS",
            quantity=50,
            amount="100.00",
            validity_days=30,
        )
        youtube_dl = MagicMock()
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Task video",
            "thumbnail": "",
            "duration": None,
        }
        with patch("engager.views.safe_public_url", return_value=True), patch.dict(
            "sys.modules", {"yt_dlp": SimpleNamespace(YoutubeDL=youtube_dl)}
        ):
            response = self.client.post(
                "/api/campaigns/",
                {
                    "platform": "INSTAGRAM",
                    "content_type": "REEL",
                    "objective": "FOLLOWERS",
                    "budget_package": instagram_package.pk,
                    "url": "https://www.instagram.com/reel/task-video",
                    "media_seconds": 75,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        task = Task.objects.get(campaign_id=response.data["id"])
        self.assertEqual(task.seconds, 75)

    def test_campaign_subscription_requires_instructions_and_proof_then_expires(self):
        self.client.force_authenticate(self.creator)
        create_response = self.client.post(
            "/api/payment-requests/",
            {
                "request_type": "CAMPAIGN_BUDGET",
                "amount": "100.00",
                "campaign": self.campaign.pk,
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        payment_id = create_response.data["id"]

        self.client.force_authenticate(self.engager)
        self.assertEqual(self.client.get("/api/tasks/").data, [])
        self.client.force_authenticate(self.creator)

        self.client.force_authenticate(self.superuser)
        self.client.force_authenticate(self.admin)
        instructions_denied = self.client.post(
            f"/api/admin/review/payments/{payment_id}/",
            {"status": "AWAITING_PAYMENT", "admin_payment_details": "Till 123456"},
            format="json",
        )
        self.assertEqual(instructions_denied.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.superuser)
        missing_instructions = self.client.post(
            f"/api/admin/review/payments/{payment_id}/",
            {"status": "AWAITING_PAYMENT", "admin_payment_details": "  "},
            format="json",
        )
        self.assertEqual(missing_instructions.status_code, status.HTTP_400_BAD_REQUEST)
        instructions_response = self.client.post(
            f"/api/admin/review/payments/{payment_id}/",
            {"status": "AWAITING_PAYMENT", "admin_payment_details": "Buy Goods Till 123456"},
            format="json",
        )
        self.assertEqual(instructions_response.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(self.creator)
        evidence_response = self.client.post(
            f"/api/payment-requests/{payment_id}/submit-evidence/",
            {
                "transaction_code": "MIG123456",
                "screenshot": SimpleUploadedFile(
                    "proof.png",
                    b"\x89PNG\r\n\x1a\npayment-proof",
                    content_type="image/png",
                ),
            },
            format="multipart",
        )
        self.assertEqual(evidence_response.status_code, status.HTTP_200_OK)
        evidence_name = PaymentRequest.objects.get(pk=payment_id).evidence_screenshot.name
        self.client.force_authenticate(self.engager)
        self.assertEqual(self.client.get("/api/tasks/").data, [])
        self.assertEqual(
            self.client.get(f"/api/admin/review/payments/{payment_id}/evidence/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.client.force_authenticate(self.admin)
        evidence_file_response = self.client.get(f"/api/admin/review/payments/{payment_id}/evidence/")
        self.assertEqual(evidence_file_response.status_code, status.HTTP_200_OK)
        self.assertEqual(evidence_file_response["Content-Type"], "image/png")
        evidence_file_response.close()

        with self.captureOnCommitCallbacks(execute=True):
            approve_response = self.client.post(
                f"/api/admin/review/payments/{payment_id}/",
                {"status": "APPROVED"},
                format="json",
            )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)
        self.assertFalse(default_storage.exists(evidence_name))

        self.campaign.refresh_from_db()
        self.assertGreater(self.campaign.subscription_until, timezone.now())
        self.assertLessEqual(self.campaign.subscription_until, timezone.now() + timedelta(days=30))
        self.assertEqual(self.campaign.approved_budget, self.campaign.budget)
        approved_payment = PaymentRequest.objects.get(pk=payment_id)
        self.assertEqual(approved_payment.transaction_code, "MIG123456")
        self.assertFalse(approved_payment.evidence_screenshot)

        self.client.force_authenticate(self.engager)
        self.assertEqual(self.client.get("/api/tasks/").status_code, status.HTTP_200_OK)
        Campaign.objects.filter(pk=self.campaign.pk).update(
            subscription_until=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.client.get("/api/tasks/").data, [])

    def test_admin_can_create_budget_and_question_and_questions_are_platform_specific(self):
        self.client.force_authenticate(self.admin)
        budget_response = self.client.post(
            "/api/admin/campaign-budgets/",
            {
                "platform": "INSTAGRAM",
                "objective": "FOLLOWERS",
                "quantity": 50,
                "amount": "150.00",
                "validity_days": 30,
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(budget_response.status_code, status.HTTP_201_CREATED)
        question_response = self.client.post(
            "/api/admin/questions/",
            {"platform": "YOUTUBE", "question": "Which topic was discussed?", "is_active": True},
            format="json",
        )
        self.assertEqual(question_response.status_code, status.HTTP_201_CREATED)
        EngagerQuestion.objects.create(platform="TIKTOK", question="This must not be selected.")
        self.campaign.subscription_until = timezone.now() + timedelta(days=30)
        self.campaign.target_quantity = 1
        self.campaign.save(update_fields=["subscription_until", "target_quantity"])
        PaymentRequest.objects.create(
            user=self.creator,
            request_type="CAMPAIGN_BUDGET",
            amount=self.package.amount,
            campaign=self.campaign,
            transaction_code="PAID-001",
            status="APPROVED",
        )

        self.client.force_authenticate(self.engager)
        self.client.post(f"/api/tasks/{self.task.pk}/start/", {}, format="json")
        TaskClaim.objects.filter(task=self.task, user=self.engager).update(
            started_at=timezone.now() - timedelta(seconds=self.task.seconds + 1)
        )
        claim_response = self.client.post(f"/api/tasks/{self.task.pk}/claim/", {}, format="json")
        self.assertEqual(claim_response.status_code, status.HTTP_200_OK)
        self.assertEqual(claim_response.data["question"], "Which topic was discussed?")
        self.assertEqual(len(self.client.get("/api/tasks/").data), 1)

    def test_only_superuser_can_manage_engager_levels(self):
        self.client.force_authenticate(self.admin)
        denied = self.client.post(
            "/api/admin/levels/",
            {"name": "Restricted", "fee": "300.00"},
            format="json",
        )
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.superuser)
        created = self.client.post(
            "/api/admin/levels/",
            {"name": "Superuser level", "fee": "300.00"},
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

    def test_payment_instructions_are_only_exposed_to_superusers(self):
        settings_obj = PlatformSettings.current()
        settings_obj.payment_instructions = "Private Till 123456"
        settings_obj.save(update_fields=["payment_instructions"])

        public_response = self.client.get("/api/settings/")
        self.assertEqual(public_response.status_code, status.HTTP_200_OK)
        self.assertNotIn("payment_instructions", public_response.data)

        self.client.force_authenticate(self.superuser)
        admin_response = self.client.get("/api/admin/settings/")
        self.assertEqual(admin_response.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_response.data["payment_instructions"], "Private Till 123456")

    def test_only_superuser_can_change_withdrawal_threshold(self):
        self.client.force_authenticate(self.admin)
        denied = self.client.patch(
            "/api/admin/settings/",
            {"withdrawal_threshold": "480.00"},
            format="json",
        )
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.superuser)
        invalid = self.client.patch(
            "/api/admin/settings/",
            {"withdrawal_threshold": "0.00"},
            format="json",
        )
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

        updated = self.client.patch(
            "/api/admin/settings/",
            {"withdrawal_threshold": "480.00"},
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        self.assertEqual(updated.data["withdrawal_threshold"], "480.00")
        self.assertEqual(
            self.client.get("/api/settings/").data["withdrawal_threshold"],
            "480.00",
        )

        self.client.force_authenticate(self.engager)
        below_updated_minimum = self.client.post(
            "/api/withdrawals/",
            {"amount": "120.00", "mpesa_number": "254700123456"},
            format="json",
        )
        self.assertEqual(below_updated_minimum.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Minimum withdrawal is KSh 480.00", below_updated_minimum.data["amount"])
