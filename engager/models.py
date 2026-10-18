import uuid
import os
from datetime import timedelta
from decimal import Decimal
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.utils import timezone

def ledger_reference():
    return uuid.uuid4().hex

def payment_evidence_upload_to(instance, filename):
    extension = os.path.splitext(filename)[1].lower()
    if extension not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        extension = ""
    return f"payment-evidence/{uuid.uuid4().hex}{extension}"

CONTENT_TYPE_CHOICES = [
    ("PROFILE", "Profile"), ("POST", "Post"), ("REEL", "Reel"),
    ("CHANNEL", "Channel"), ("VIDEO", "Video"), ("SHORT", "Short"),
    ("CHALLENGE", "Challenge"),
]

class User(AbstractUser):
    ROLE_CHOICES = [("CREATOR", "Creator"), ("ENGAGER", "Engager")]
    phone = models.CharField(max_length=20, blank=True)
    roles = models.JSONField(default=list, blank=True)  # may contain both CREATOR and ENGAGER
    mpesa_number = models.CharField(max_length=20, blank=True)
    referral_code = models.CharField(max_length=16, unique=True, blank=True)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals")
    fund_password_hash = models.CharField(max_length=128, blank=True)  # store a hash only; never plaintext
    creator_active = models.BooleanField(default=False)
    engager_active = models.BooleanField(default=False)
    selected_level = models.ForeignKey("SubscriptionLevel", null=True, blank=True, on_delete=models.SET_NULL)

    def save(self, *args, **kwargs):
        if not self.referral_code:
            self.referral_code = uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    @property
    def is_creator(self):
        return "CREATOR" in (self.roles or [])

    @property
    def is_engager(self):
        return "ENGAGER" in (self.roles or [])

class PlatformSettings(models.Model):
    creator_activation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("200.00"))
    free_campaign_slots = models.PositiveIntegerField(default=2)
    referral_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("10.00"))
    withdrawal_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("120.00"))
    default_task_seconds = models.PositiveIntegerField(default=20)
    payment_instructions = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Platform settings"

    @classmethod
    def current(cls):
        obj = cls.objects.first()
        return obj or cls.objects.create()

class SubscriptionLevel(models.Model):
    name = models.CharField(max_length=40, unique=True)
    fee = models.DecimalField(max_digits=10, decimal_places=2)
    sample_daily_earning = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sample_30_day_earning = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sample_60_day_earning = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    def __str__(self):
        return f"{self.name} (KSh {self.fee})"

class CampaignBudget(models.Model):
    PLATFORMS = [("FACEBOOK","Facebook"),("INSTAGRAM","Instagram"),("TIKTOK","TikTok"),("YOUTUBE","YouTube")]
    OBJECTIVES = [
        ("FOLLOWERS","Followers"),("VIEWERS","Viewers"),("LIKES","Likes"),
        ("COMMENTS","Comments"),("SUBSCRIBERS","Subscribers"),
        ("WEBSITE_VISITS","Website visits"),("SURVEY","Survey/feedback"),
        ("QUIZ","Educational quiz"),("REFERRALS","Legitimate referrals"),
    ]
    platform = models.CharField(max_length=20, choices=PLATFORMS)
    objective = models.CharField(max_length=24, choices=OBJECTIVES)
    quantity = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    validity_days = models.PositiveIntegerField(default=30)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["platform", "objective", "quantity"]

    def __str__(self):
        return f"{self.platform} {self.quantity} {self.objective} / KSh {self.amount}"

class EngagerQuestion(models.Model):
    PLATFORMS = CampaignBudget.PLATFORMS
    platform = models.CharField(max_length=20, choices=PLATFORMS)
    question = models.CharField(max_length=500)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["platform", "id"]

    def __str__(self):
        return f"{self.platform}: {self.question[:80]}"

class Campaign(models.Model):
    PLATFORMS = [("FACEBOOK","Facebook"),("INSTAGRAM","Instagram"),("TIKTOK","TikTok"),("YOUTUBE","YouTube")]
    CONTENT_TYPES = {
        "FACEBOOK": [("PROFILE", "Profile"), ("POST", "Post"), ("REEL", "Reel")],
        "INSTAGRAM": [("PROFILE", "Profile"), ("POST", "Post"), ("REEL", "Reel")],
        "TIKTOK": [("PROFILE", "Profile"), ("CHALLENGE", "Challenge")],
        "YOUTUBE": [("CHANNEL", "Channel"), ("VIDEO", "Video"), ("SHORT", "Short")],
    }
    VIDEO_CONTENT_TYPES = {"REEL", "CHALLENGE", "VIDEO", "SHORT"}
    OBJECTIVES = [
        ("FOLLOWERS","Followers"),("VIEWERS","Viewers"),("LIKES","Likes"),
        ("COMMENTS","Comments"),("SUBSCRIBERS","Subscribers"),
        ("WEBSITE_VISITS","Website visits"),("SURVEY","Survey/feedback"),
        ("QUIZ","Educational quiz"),("REFERRALS","Legitimate referrals"),
    ]
    STATUSES = [("DRAFT","Draft"),("PENDING","Pending review"),("APPROVED","Approved"),("REJECTED","Rejected"),("PAUSED","Paused"),("REMOVED","Removed by admin")]
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="campaigns")
    platform = models.CharField(max_length=20, choices=PLATFORMS)
    content_type = models.CharField(max_length=12, choices=CONTENT_TYPE_CHOICES, default="VIDEO")
    objective = models.CharField(max_length=24, choices=OBJECTIVES)
    url = models.URLField(max_length=1000)
    profile_url = models.URLField(max_length=1000, blank=True)
    title = models.CharField(max_length=300, blank=True)
    thumbnail_url = models.URLField(max_length=1000, blank=True)
    media_seconds = models.PositiveIntegerField(null=True, blank=True)
    description = models.TextField(blank=True)
    budget = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    approved_budget = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    budget_package = models.ForeignKey(CampaignBudget, null=True, blank=True, on_delete=models.PROTECT, related_name="campaigns")
    target_quantity = models.PositiveIntegerField(default=0)
    subscription_days = models.PositiveIntegerField(default=30)
    subscription_until = models.DateTimeField(null=True, blank=True)
    is_free_trial = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=STATUSES, default="PENDING")
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        ordering = ["-created_at"]

    @property
    def task_seconds(self):
        if self.content_type in self.VIDEO_CONTENT_TYPES:
            return self.media_seconds or 20
        return 20

    def __str__(self):
        return f"{self.platform}: {self.title or self.url[:50]}"
    def activate_subscription(self):
        now = timezone.now()
        start = self.subscription_until if self.subscription_until and self.subscription_until > now else now
        self.subscription_until = start + timedelta(days=self.subscription_days)
        self.approved_budget = self.budget
        self.save(update_fields=["subscription_until", "approved_budget"])

class PaymentRequest(models.Model):
    TYPES = [("CREATOR_ACTIVATION","Creator activation"),("ENGAGER_LEVEL","Engager subscription"),("CAMPAIGN_BUDGET","Campaign budget")]
    STATUSES = [
        ("PENDING","Pending admin instructions"),
        ("AWAITING_PAYMENT","Awaiting customer payment"),
        ("UNDER_REVIEW","Payment evidence submitted"),
        ("APPROVED","Approved"),
        ("REJECTED","Rejected"),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payment_requests")
    request_type = models.CharField(max_length=24, choices=TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_code = models.CharField(max_length=80, blank=True)
    evidence_note = models.TextField(blank=True)
    evidence_screenshot = models.FileField(upload_to=payment_evidence_upload_to, blank=True)
    admin_payment_details = models.TextField(blank=True)
    level = models.ForeignKey(SubscriptionLevel, null=True, blank=True, on_delete=models.SET_NULL)
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.CASCADE, related_name="payment_requests")
    status = models.CharField(max_length=20, choices=STATUSES, default="PENDING")
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    def __str__(self):
        return f"{self.user.username} / {self.request_type} / {self.amount}"

class Task(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="tasks")
    prompt = models.CharField(max_length=300, default="Open the campaign and answer the question.")
    question = models.CharField(max_length=500, blank=True)
    expected_answer = models.CharField(max_length=300, blank=True)
    seconds = models.PositiveIntegerField(default=20)
    is_active = models.BooleanField(default=True)
    def __str__(self):
        return f"Task for campaign #{self.campaign_id}"

class TaskClaim(models.Model):
    STATUSES = [("STARTED","Started"),("CLAIMED","Claimed"),("SUBMITTED","Submitted"),("APPROVED","Approved"),("REJECTED","Rejected")]
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="claims")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_claims")
    started_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    question = models.CharField(max_length=500, blank=True)
    answer = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default="CLAIMED")
    admin_note = models.TextField(blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["task","user"], name="unique_task_claim_per_user")]
    def __str__(self):
        return f"{self.user.username}: task {self.task_id}"

class WalletLedger(models.Model):
    TYPES = [("CREDIT","Credit"),("DEBIT","Debit"),("RESERVE","Reserve"),("RELEASE","Release"),("ADJUSTMENT","Adjustment")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ledger_entries")
    entry_type = models.CharField(max_length=12, choices=TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=80, default=ledger_reference, unique=True)
    description = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at"]

class WithdrawalRequest(models.Model):
    STATUSES = [("PENDING","Pending"),("APPROVED","Approved"),("PAID","Paid"),("REJECTED","Rejected")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="withdrawals")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    mpesa_number = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=STATUSES, default="PENDING")
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    def __str__(self):
        return f"{self.user.username}: KSh {self.amount}"
