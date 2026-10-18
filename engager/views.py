from decimal import Decimal
from datetime import timedelta
import logging
import json
import random
import mimetypes
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from django.http import FileResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, IntegerField, Q, Sum, Value, When
from rest_framework import generics, viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import User, PlatformSettings, SubscriptionLevel, CampaignBudget, EngagerQuestion, Campaign, PaymentRequest, Task, TaskClaim, WithdrawalRequest, WalletLedger
from .serializers import RegisterSerializer, UserSerializer, SettingsSerializer, LevelSerializer, CampaignBudgetSerializer, EngagerQuestionSerializer, CampaignSerializer, PaymentRequestSerializer, TaskSerializer, TaskClaimSerializer, WithdrawalSerializer

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = {
    "youtube.com","www.youtube.com","youtu.be","m.youtube.com",
    "facebook.com","www.facebook.com","fb.watch",
    "instagram.com","www.instagram.com",
    "tiktok.com","www.tiktok.com","vm.tiktok.com",
    "httpbin.org", # remove this demo host before deployment
}

def safe_public_url(url):
    try:
        p = urlparse(url)
        return p.scheme == "https" and p.hostname and p.hostname.lower() in ALLOWED_HOSTS
    except Exception:
        return False

def fetch_youtube_oembed_metadata(url):
    query = urlencode({"url": url, "format": "json"})
    request = Request(
        f"https://www.youtube.com/oembed?{query}",
        headers={"User-Agent": "MIGSTAR-ENGAGER/1.0"},
    )
    with urlopen(request, timeout=5) as response:
        payload = response.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("YouTube oEmbed response exceeded the size limit.")
    data = json.loads(payload)
    return {
        "title": data.get("title") or "",
        "thumbnail": data.get("thumbnail_url") or "",
    }

def demo_ledger_balance(user):
    ledger = WalletLedger.objects.filter(user=user)
    return sum(
        (entry.amount if entry.entry_type in ("CREDIT","RELEASE","ADJUSTMENT") else -entry.amount for entry in ledger),
        start=Decimal("0.00"),
    )

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

class MeView(APIView):
    def get(self, request):
        data = UserSerializer(request.user).data
        data["demo_balance"] = str(demo_ledger_balance(request.user))
        data["notice"] = "Demonstration balance only; not a bank or M-Pesa balance."
        if request.user.is_creator:
            settings_obj = PlatformSettings.current()
            used_campaigns = Campaign.objects.filter(creator=request.user).count()
            data["free_campaign_slots"] = settings_obj.free_campaign_slots
            data["free_campaigns_remaining"] = max(settings_obj.free_campaign_slots - used_campaigns, 0)
        else:
            data["free_campaign_slots"] = 0
            data["free_campaigns_remaining"] = 0
        return Response(data)

class SwitchRoleView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        role = request.data.get("role")
        if role not in ("CREATOR", "ENGAGER"):
            raise ValidationError({"role": "Choose CREATOR or ENGAGER."})
        roles = list(dict.fromkeys(request.user.roles or []))
        if role not in roles:
            roles.append(role)
            request.user.roles = roles
            request.user.save(update_fields=["roles"])
        return Response({"roles": roles})

class SettingsView(APIView):
    permission_classes = [permissions.AllowAny]
    def get(self, request):
        data = SettingsSerializer(PlatformSettings.current()).data
        data.pop("payment_instructions", None)
        return Response(data)

class LevelListView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = LevelSerializer
    queryset = SubscriptionLevel.objects.filter(is_active=True).order_by("sort_order","fee")

class CampaignBudgetListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CampaignBudgetSerializer
    def get_queryset(self):
        if not self.request.user.is_creator:
            raise PermissionDenied("Campaign packages are only available to creators.")
        return CampaignBudget.objects.filter(is_active=True)

class CampaignViewSet(viewsets.ModelViewSet):
    serializer_class = CampaignSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]
    def get_queryset(self):
        if self.request.user.is_staff:
            return Campaign.objects.all()
        if self.request.user.is_creator:
            return Campaign.objects.filter(creator=self.request.user)
        campaigns = Campaign.objects.annotate(
            active_claim_count=Count(
                "tasks__claims",
                filter=Q(tasks__claims__status__in=("STARTED", "CLAIMED", "SUBMITTED", "APPROVED")),
                distinct=True,
            ),
            purchased_units=(
                Count(
                    "payment_requests",
                    filter=Q(payment_requests__request_type="CAMPAIGN_BUDGET", payment_requests__status="APPROVED"),
                    distinct=True,
                ) + Case(
                    When(is_free_trial=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ) * F("target_quantity"),
        )
        return campaigns.filter(
            (Q(status="APPROVED") & (
                Q(budget_package__isnull=True) | Q(
                    subscription_until__gt=timezone.now(),
                    active_claim_count__lt=F("purchased_units"),
                )
            )) | Q(creator=self.request.user)
        ).distinct()
    def perform_create(self, serializer):
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=self.request.user.pk)
            if not user.creator_active:
                raise ValidationError({"detail": "Your creator account must be active before submitting campaigns."})
            settings_obj = PlatformSettings.current()
            used_campaigns = Campaign.objects.filter(creator=user).count()
            obj = serializer.save(
                creator=user,
                is_free_trial=used_campaigns < settings_obj.free_campaign_slots,
            )
        # Best-effort metadata extraction; never download media.
        if not safe_public_url(obj.url):
            obj.status = "REJECTED"
            obj.admin_note = "URL must be HTTPS and from a supported public platform domain."
            obj.save(update_fields=["status","admin_note"])
            return
        try:
            import yt_dlp
            opts = {"skip_download": True, "quiet": True, "no_warnings": True, "extract_flat": False, "socket_timeout": 8, "noplaylist": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(obj.url, download=False)
            obj.title = (info.get("title") or "")[:300]
            obj.thumbnail_url = info.get("thumbnail") or ""
            duration = int(info.get("duration") or 0) or None
            if obj.content_type in Campaign.VIDEO_CONTENT_TYPES and duration:
                obj.media_seconds = duration
            obj.save(update_fields=["title","thumbnail_url","media_seconds"])
        except Exception as error:
            logger.warning(
                "Campaign metadata extractor failed (campaign_id=%s platform=%s content_type=%s error_type=%s)",
                obj.pk,
                obj.platform,
                obj.content_type,
                type(error).__name__,
            )
            info = {}
            if obj.platform == "YOUTUBE":
                try:
                    info = fetch_youtube_oembed_metadata(obj.url)
                except Exception as fallback_error:
                    logger.warning(
                        "YouTube oEmbed fallback failed (campaign_id=%s error_type=%s)",
                        obj.pk,
                        type(fallback_error).__name__,
                    )
            obj.title = (info.get("title") or "")[:300]
            obj.thumbnail_url = info.get("thumbnail") or ""
            update_fields = ["title", "thumbnail_url"]
            if obj.content_type in Campaign.VIDEO_CONTENT_TYPES and not obj.media_seconds:
                obj.admin_note = "Video details were partially fetched; enter the duration manually."
                update_fields.append("admin_note")
            elif not obj.title and not obj.thumbnail_url:
                obj.admin_note = "Metadata could not be fetched automatically; please review the URL manually."
                update_fields.append("admin_note")
            obj.save(update_fields=update_fields)
        if obj.content_type in Campaign.VIDEO_CONTENT_TYPES and not obj.media_seconds:
            obj.delete()
            raise ValidationError({"media_seconds": "Video duration could not be fetched. Enter the duration in seconds and submit again."})
        Task.objects.get_or_create(campaign=obj, defaults={"seconds": obj.task_seconds})
        if not obj.is_free_trial:
            PaymentRequest.objects.create(
                user=obj.creator,
                request_type="CAMPAIGN_BUDGET",
                amount=obj.budget,
                campaign=obj,
            )
    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAuthenticated])
    def submit(self, request, pk=None):
        campaign = self.get_object()
        if campaign.creator_id != request.user.id and not request.user.is_staff:
            return Response({"detail":"Not your campaign."}, status=403)
        campaign.status = "PENDING"
        campaign.save(update_fields=["status"])
        return Response(CampaignSerializer(campaign).data)

class PaymentRequestViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentRequestSerializer
    http_method_names = ["get","post","head","options"]
    def get_queryset(self):
        return PaymentRequest.objects.all() if self.request.user.is_staff else PaymentRequest.objects.filter(user=self.request.user)
    def get_permissions(self):
        return [permissions.IsAuthenticated()]
    @action(detail=True, methods=["post"], url_path="submit-evidence")
    def submit_evidence(self, request, pk=None):
        payment = self.get_object()
        if payment.user_id != request.user.id:
            return Response({"detail": "This payment request does not belong to you."}, status=403)
        if payment.status != "AWAITING_PAYMENT":
            return Response({"detail": "Payment evidence can only be submitted after payment instructions are provided."}, status=409)
        transaction_code = str(request.data.get("transaction_code", "")).strip()
        if not transaction_code or len(transaction_code) > 80:
            return Response({"transaction_code": "Enter a valid transaction code (up to 80 characters)."}, status=400)
        if not request.FILES.get("screenshot"):
            return Response({"screenshot": "A payment screenshot is required."}, status=400)
        serializer = PaymentRequestSerializer(
            payment,
            data={"screenshot": request.FILES.get("screenshot")},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        old_evidence = payment.evidence_screenshot
        payment.evidence_screenshot = serializer.validated_data["screenshot"]
        payment.transaction_code = transaction_code
        payment.status = "UNDER_REVIEW"
        payment.save(update_fields=["evidence_screenshot", "transaction_code", "status"])
        if old_evidence:
            old_evidence.delete(save=False)
        return Response(PaymentRequestSerializer(payment, context={"request": request}).data)

class TaskViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TaskSerializer
    permission_classes = [permissions.IsAuthenticated]
    def get_queryset(self):
        now = timezone.now()
        tasks = Task.objects.annotate(
            active_claim_count=Count(
                "campaign__tasks__claims",
                filter=Q(campaign__tasks__claims__status__in=("STARTED", "CLAIMED", "SUBMITTED", "APPROVED")),
                distinct=True,
            ),
            purchased_units=Count(
                "campaign__payment_requests",
                filter=Q(campaign__payment_requests__request_type="CAMPAIGN_BUDGET", campaign__payment_requests__status="APPROVED"),
                distinct=True,
            ) * F("campaign__target_quantity") + Case(
                When(campaign__is_free_trial=True, then=F("campaign__target_quantity")),
                default=Value(0),
                output_field=IntegerField(),
            ),
        )
        return tasks.filter(
            is_active=True,
            campaign__status="APPROVED",
        ).filter(
            Q(campaign__budget_package__isnull=True) | (
                Q(campaign__subscription_until__gt=now) & (
                    Q(active_claim_count__lt=F("purchased_units")) |
                    Q(claims__user=self.request.user, claims__status__in=("STARTED", "CLAIMED", "SUBMITTED"))
                )
            )
        ).distinct()
    @action(detail=True, methods=["post"])
    @transaction.atomic
    def start(self, request, pk=None):
        task = self.get_object()
        if not request.user.engager_active:
            return Response({"detail":"Your engager account is not active yet."}, status=403)
        existing = TaskClaim.objects.filter(task=task, user=request.user).first()
        if existing:
            if existing.status == "STARTED":
                return Response(TaskClaimSerializer(existing).data)
            return Response({"detail":"You have already claimed this task."}, status=409)
        campaign = Campaign.objects.select_for_update().get(pk=task.campaign_id)
        if campaign.budget_package_id:
            approved_packages = PaymentRequest.objects.filter(
                campaign=campaign,
                request_type="CAMPAIGN_BUDGET",
                status="APPROVED",
            ).count()
            active_claims = TaskClaim.objects.filter(
                task__campaign=campaign,
                status__in=("STARTED", "CLAIMED", "SUBMITTED", "APPROVED"),
            ).count()
            available_units = (approved_packages + int(campaign.is_free_trial)) * campaign.target_quantity
            if active_claims >= available_units:
                return Response({"detail":"This campaign has reached its available task quantity."}, status=409)
        try:
            claim = TaskClaim.objects.create(task=task, user=request.user, started_at=timezone.now(), status="STARTED")
        except IntegrityError:
            return Response({"detail":"You have already claimed this task."}, status=409)
        return Response(TaskClaimSerializer(claim).data, status=201)
    @action(detail=True, methods=["post"])
    @transaction.atomic
    def claim(self, request, pk=None):
        task = get_object_or_404(Task.objects.select_related("campaign"), pk=pk)
        if not request.user.engager_active:
            return Response({"detail":"Your engager account is not active yet."}, status=403)
        claim = TaskClaim.objects.select_for_update().filter(task=task, user=request.user).first()
        if not claim:
            return Response({"detail":"Open the content to start this task first."}, status=400)
        if claim.status == "CLAIMED":
            return Response(TaskClaimSerializer(claim).data)
        if claim.status != "STARTED" or not claim.started_at:
            return Response({"detail":"This task cannot be claimed in its current state."}, status=409)
        remaining_seconds = task.seconds - (timezone.now() - claim.started_at).total_seconds()
        if remaining_seconds > 0:
            return Response(
                {"detail": f"Please watch the content for {int(remaining_seconds) + 1} more seconds before claiming this task."},
                status=400,
            )
        questions = list(EngagerQuestion.objects.filter(
            platform=task.campaign.platform,
            is_active=True,
        ).values_list("question", flat=True))
        if not questions:
            return Response({"detail": "No active admin question is configured for this platform yet."}, status=409)
        claim.question = random.choice(questions)
        claim.status = "CLAIMED"
        claim.claimed_at = timezone.now()
        claim.save(update_fields=["question", "status", "claimed_at"])
        return Response(TaskClaimSerializer(claim).data)
    @action(detail=True, methods=["post"])
    def answer(self, request, pk=None):
        task = get_object_or_404(Task, pk=pk)
        claim = TaskClaim.objects.filter(task=task, user=request.user).first()
        if not claim:
            return Response({"detail":"Claim this task first."}, status=400)
        if claim.status == "STARTED":
            return Response({"detail":"Wait for the viewing time, then claim the task before answering."}, status=400)
        if claim.status != "CLAIMED":
            return Response({"detail":"This task was already submitted or reviewed."}, status=409)
        answer = str(request.data.get("answer","")).strip()
        if not answer:
            return Response({"detail":"Answer is required."}, status=400)
        claim.answer = answer[:2000]
        claim.status = "SUBMITTED"
        claim.submitted_at = timezone.now()
        claim.save(update_fields=["answer","status","submitted_at"])
        return Response(TaskClaimSerializer(claim).data)

class MyClaimsView(generics.ListAPIView):
    serializer_class = TaskClaimSerializer
    permission_classes = [permissions.IsAuthenticated]
    def get_queryset(self):
        return TaskClaim.objects.filter(user=self.request.user).select_related("task","task__campaign")

class WithdrawalViewSet(viewsets.ModelViewSet):
    serializer_class = WithdrawalSerializer
    http_method_names = ["get","post","head","options"]
    def get_queryset(self):
        return WithdrawalRequest.objects.all() if self.request.user.is_staff else WithdrawalRequest.objects.filter(user=self.request.user)
    @transaction.atomic
    def perform_create(self, serializer):
        settings_obj = PlatformSettings.current()
        amount = serializer.validated_data["amount"]
        if amount < settings_obj.withdrawal_threshold:
            raise ValidationError({"amount": f"Minimum withdrawal is KSh {settings_obj.withdrawal_threshold}."})
        user = User.objects.select_for_update().get(pk=self.request.user.pk)
        outstanding = WithdrawalRequest.objects.filter(
            user=user,
            status__in=("PENDING","APPROVED"),
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        available = demo_ledger_balance(user) - outstanding
        if amount > available:
            raise ValidationError({"amount": f"Withdrawal exceeds your available demo balance of KSh {available}."})
        serializer.save(user=self.request.user)

class WalletView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        entries = WalletLedger.objects.filter(user=request.user).values("entry_type","amount","reference","description","created_at")
        return Response({"demo_balance":str(demo_ledger_balance(request.user)),"currency":"KES","is_real_money":False,"entries":list(entries)})

class WalletFundCampaignView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        if not request.user.is_creator or not request.user.is_engager:
            raise PermissionDenied("Both creator and engager roles are required to fund a campaign from this wallet.")

        try:
            campaign_id = int(request.data.get("campaign"))
        except (TypeError, ValueError):
            raise ValidationError({"campaign": "Choose a valid campaign."})

        user = User.objects.select_for_update().get(pk=request.user.pk)
        if not user.creator_active:
            raise PermissionDenied("Your creator account must be active to fund a campaign.")
        campaign = get_object_or_404(
            Campaign.objects.select_for_update(),
            pk=campaign_id,
            creator=user,
        )
        if campaign.status != "APPROVED" or not campaign.budget_package_id:
            raise ValidationError({"campaign": "Only approved campaigns with an admin-configured package can be funded."})
        if campaign.subscription_until and campaign.subscription_until > timezone.now():
            raise ValidationError({"campaign": "This campaign subscription is already active."})
        if PaymentRequest.objects.filter(
            campaign=campaign,
            request_type="CAMPAIGN_BUDGET",
            status__in=("PENDING", "AWAITING_PAYMENT", "UNDER_REVIEW"),
        ).exists():
            raise ValidationError({"campaign": "A campaign payment request is already pending."})

        amount = campaign.budget
        balance = demo_ledger_balance(user)
        if amount <= 0:
            raise ValidationError({"campaign": "The campaign package must have a positive price."})
        if balance < amount:
            raise ValidationError({"campaign": f"Insufficient engager wallet funds. Available balance: KSh {balance}."})

        WalletLedger.objects.create(
            user=user,
            entry_type="DEBIT",
            amount=amount,
            description=f"Wallet-funded campaign subscription #{campaign.pk}",
        )
        PaymentRequest.objects.create(
            user=user,
            request_type="CAMPAIGN_BUDGET",
            amount=amount,
            campaign=campaign,
            status="APPROVED",
            admin_note="Funded from the engager wallet.",
            reviewed_at=timezone.now(),
        )
        campaign.subscription_until = timezone.now() + timedelta(days=campaign.subscription_days)
        campaign.approved_budget = campaign.budget
        campaign.save(update_fields=["subscription_until", "approved_budget"])
        return Response({
            "campaign": campaign.pk,
            "demo_balance": str(demo_ledger_balance(user)),
            "subscription_until": campaign.subscription_until,
        })

class IsStaffUser(permissions.BasePermission):
    message = "Admin access required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


class StaffOnlyMixin:
    permission_classes = [permissions.IsAuthenticated, IsStaffUser]

class IsSuperuser(permissions.BasePermission):
    message = "Superuser access required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)

class AdminCampaignBudgetViewSet(viewsets.ModelViewSet):
    queryset = CampaignBudget.objects.all()
    serializer_class = CampaignBudgetSerializer
    permission_classes = [permissions.IsAuthenticated, IsStaffUser]

class AdminQuestionViewSet(viewsets.ModelViewSet):
    queryset = EngagerQuestion.objects.all()
    serializer_class = EngagerQuestionSerializer
    permission_classes = [permissions.IsAuthenticated, IsStaffUser]

class AdminLevelViewSet(viewsets.ModelViewSet):
    queryset = SubscriptionLevel.objects.all().order_by("sort_order", "fee")
    serializer_class = LevelSerializer
    permission_classes = [permissions.IsAuthenticated, IsSuperuser]

class AdminSettingsView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsSuperuser]

    def get(self, request):
        return Response(SettingsSerializer(PlatformSettings.current()).data)

    def patch(self, request):
        settings_obj = PlatformSettings.current()
        serializer = SettingsSerializer(settings_obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

class AdminCampaignReviewView(StaffOnlyMixin, APIView):
    def get(self, request):
        campaigns = Campaign.objects.select_related("creator").prefetch_related("payment_requests").order_by("-created_at")
        data = []
        for campaign in campaigns:
            campaign_payments = [
                payment for payment in campaign.payment_requests.all()
                if payment.request_type == "CAMPAIGN_BUDGET"
            ]
            latest_payment = max(campaign_payments, key=lambda payment: payment.created_at, default=None)
            data.append({
                "id": campaign.id,
                "creator_username": campaign.creator.username,
                "platform": campaign.platform,
                "content_type": campaign.content_type,
                "objective": campaign.objective,
                "title": campaign.title or "Untitled campaign",
                "url": campaign.url,
                "media_seconds": campaign.media_seconds,
                "budget": str(campaign.budget),
                "approved_budget": str(campaign.approved_budget),
                "budget_package": campaign.budget_package_id,
                "is_free_trial": campaign.is_free_trial,
                "target_quantity": campaign.target_quantity,
                "subscription_days": campaign.subscription_days,
                "subscription_until": campaign.subscription_until.isoformat() if campaign.subscription_until else None,
                "payment_status": latest_payment.status if latest_payment else None,
                "status": campaign.status,
                "admin_note": campaign.admin_note,
                "created_at": campaign.created_at.isoformat(),
            })
        return Response(data)

    @transaction.atomic
    def post(self, request, pk):
        campaign = get_object_or_404(Campaign.objects.select_for_update(), pk=pk)
        new_status = request.data.get("status")
        if new_status not in dict(Campaign.STATUSES):
            return Response({"detail": "Choose a valid campaign status."}, status=400)
        if new_status == "APPROVED" and not campaign.is_free_trial:
            payment_approved = PaymentRequest.objects.filter(
                campaign=campaign,
                request_type="CAMPAIGN_BUDGET",
                status="APPROVED",
            ).exists()
            if not payment_approved:
                return Response(
                    {"detail": "Verify the campaign payment before approving this paid campaign."},
                    status=409,
                )
        if new_status == "REMOVED":
            notice = str(request.data.get("admin_note") or "").strip()
            if campaign.status != "APPROVED":
                return Response({"detail": "Only approved campaigns can be removed from the campaign inventory."}, status=409)
            if not notice:
                return Response({"admin_note": "Provide a notice for the creator before removing this campaign."}, status=400)
            campaign.admin_note = notice
        else:
            campaign.admin_note = str(request.data.get("admin_note") or campaign.admin_note)

        campaign.status = new_status
        if new_status in {"APPROVED", "REJECTED", "REMOVED"}:
            campaign.reviewed_at = timezone.now()
        campaign.save(update_fields=["status", "admin_note", "reviewed_at"])
        if new_status == "APPROVED" and campaign.is_free_trial:
            campaign.activate_subscription()
        return Response({
            "id": campaign.id,
            "status": campaign.status,
            "admin_note": campaign.admin_note,
            "approved_budget": str(campaign.approved_budget),
        })

class AdminPaymentReviewView(StaffOnlyMixin, APIView):
    def get(self, request):
        payments = PaymentRequest.objects.select_related("user", "level").order_by("-created_at")
        data = []
        for item in payments:
            data.append({
                "id": item.id,
                "user": item.user.username,
                "request_type": item.request_type,
                "amount": str(item.amount),
                "transaction_code": item.transaction_code,
                "evidence_note": item.evidence_note,
                "evidence_screenshot_url": request.build_absolute_uri(f"/api/admin/review/payments/{item.id}/evidence/") if item.evidence_screenshot else None,
                "admin_payment_details": item.admin_payment_details,
                "campaign": item.campaign_id,
                "level": item.level_id,
                "status": item.status,
                "admin_note": item.admin_note,
                "created_at": item.created_at.isoformat(),
            })
        return Response(data)

    @transaction.atomic
    def post(self, request, pk):
        payment = get_object_or_404(PaymentRequest.objects.select_for_update(), pk=pk)
        new_status = request.data.get("status")
        if new_status not in {"AWAITING_PAYMENT", "APPROVED", "REJECTED"}:
            return Response({"detail": "Choose a valid payment status."}, status=400)

        if new_status == "AWAITING_PAYMENT":
            if not request.user.is_superuser:
                return Response({"detail": "Only a superuser can provide payment instructions."}, status=403)
            details = str(request.data.get("admin_payment_details") or "").strip()
            if payment.status != "PENDING" or not details:
                return Response({"detail": "Payment instructions are required for a pending request."}, status=400)
            payment.admin_payment_details = details
            payment.status = "AWAITING_PAYMENT"
            payment.save(update_fields=["admin_payment_details", "status"])
            return Response({"id": payment.id, "status": payment.status})

        if new_status == "APPROVED":
            if payment.status != "UNDER_REVIEW" or not payment.transaction_code or not payment.evidence_screenshot:
                return Response({"detail": "Approval requires submitted transaction code and screenshot evidence."}, status=400)
        elif new_status == "REJECTED":
            if payment.status in ("APPROVED", "REJECTED"):
                return Response({"detail": "This payment request has already been decided."}, status=409)

        payment.status = new_status
        payment.admin_note = str(request.data.get("admin_note") or payment.admin_note)
        payment.reviewed_at = timezone.now()

        if new_status == "APPROVED":
            if payment.request_type == "CREATOR_ACTIVATION":
                payment.user.creator_active = True
                payment.user.save(update_fields=["creator_active"])
            elif payment.request_type == "ENGAGER_LEVEL" and payment.level:
                payment.user.selected_level = payment.level
                payment.user.engager_active = True
                payment.user.save(update_fields=["selected_level", "engager_active"])
            elif payment.request_type == "CAMPAIGN_BUDGET" and payment.campaign:
                campaign = payment.campaign
                campaign.activate_subscription()
        evidence_storage = payment.evidence_screenshot.storage if payment.evidence_screenshot else None
        evidence_name = payment.evidence_screenshot.name if payment.evidence_screenshot else ""
        if new_status == "APPROVED" and evidence_name:
            payment.evidence_screenshot = ""
        payment.save(update_fields=["status", "admin_note", "reviewed_at", "evidence_screenshot"])
        if new_status == "APPROVED" and evidence_storage and evidence_name:
            transaction.on_commit(lambda: evidence_storage.delete(evidence_name))
        return Response({
            "id": payment.id,
            "status": payment.status,
            "admin_note": payment.admin_note,
        })

class AdminPaymentEvidenceView(StaffOnlyMixin, APIView):
    def get(self, request, pk):
        payment = get_object_or_404(PaymentRequest, pk=pk)
        if not payment.evidence_screenshot:
            return Response({"detail": "No payment screenshot is available."}, status=404)
        content_type = mimetypes.guess_type(payment.evidence_screenshot.name)[0] or "application/octet-stream"
        return FileResponse(payment.evidence_screenshot.open("rb"), content_type=content_type)

class AdminWithdrawalReviewView(StaffOnlyMixin, APIView):
    def get(self, request):
        withdrawals = WithdrawalRequest.objects.select_related("user").order_by("-id")
        return Response([
            {
                "id": item.id,
                "user": item.user.username,
                "amount": str(item.amount),
                "mpesa_number": item.mpesa_number,
                "status": item.status,
                "admin_note": item.admin_note,
                "created_at": item.created_at.isoformat(),
            }
            for item in withdrawals
        ])

    def post(self, request, pk):
        withdrawal = get_object_or_404(WithdrawalRequest, pk=pk)
        new_status = request.data.get("status")
        if new_status not in dict(WithdrawalRequest.STATUSES):
            return Response({"detail": "Choose a valid withdrawal status."}, status=400)

        withdrawal.status = new_status
        withdrawal.admin_note = str(request.data.get("admin_note") or withdrawal.admin_note)
        withdrawal.reviewed_at = timezone.now()
        withdrawal.save(update_fields=["status", "admin_note", "reviewed_at"])
        return Response({"id": withdrawal.id, "status": withdrawal.status, "admin_note": withdrawal.admin_note})

class AdminTaskAnswerReviewView(StaffOnlyMixin, APIView):
    def get(self, request):
        claims = TaskClaim.objects.select_related("task", "task__campaign", "user", "user__selected_level").order_by("-submitted_at", "-claimed_at")
        return Response([
            {
                "id": claim.id,
                "user": claim.user.username,
                "task_id": claim.task.id,
                "campaign_title": claim.task.campaign.title or claim.task.campaign.url,
                "question": claim.question,
                "answer": claim.answer,
                "status": claim.status,
                "admin_note": claim.admin_note,
                "earning_on_approval": str(claim.user.selected_level.sample_daily_earning) if claim.user.selected_level_id else None,
                "submitted_at": claim.submitted_at.isoformat() if claim.submitted_at else None,
            }
            for claim in claims
        ])

    @transaction.atomic
    def post(self, request, pk):
        claim = get_object_or_404(TaskClaim.objects.select_for_update(), pk=pk)
        new_status = request.data.get("status")
        if new_status not in {"APPROVED", "REJECTED"}:
            return Response({"detail": "Choose APPROVED or REJECTED."}, status=400)
        if claim.status == new_status:
            return Response({"id": claim.id, "status": claim.status, "admin_note": claim.admin_note})
        if claim.status != "SUBMITTED":
            return Response({"detail": "Only submitted task answers can be reviewed."}, status=409)

        earning_awarded = None
        if new_status == "APPROVED":
            user = User.objects.select_for_update().select_related("selected_level").get(pk=claim.user_id)
            if not user.selected_level_id:
                return Response({"detail": "This engager has no subscribed level, so an earning cannot be awarded."}, status=409)
            earning_awarded = user.selected_level.sample_daily_earning
            reference = f"task-claim-{claim.id}"
            WalletLedger.objects.get_or_create(
                reference=reference,
                defaults={
                    "user": user,
                    "entry_type": "CREDIT",
                    "amount": earning_awarded,
                    "description": f"Approved task #{claim.task_id} earning",
                },
            )

        claim.status = new_status
        claim.admin_note = str(request.data.get("admin_note") or claim.admin_note)
        claim.save(update_fields=["status", "admin_note"])
        result = {"id": claim.id, "status": claim.status, "admin_note": claim.admin_note}
        if earning_awarded is not None:
            result["earning_awarded"] = str(earning_awarded)
        return Response(result)
