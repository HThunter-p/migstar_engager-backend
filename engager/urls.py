from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import MeView, SwitchRoleView, SettingsView, AdminSettingsView, LevelListView, CampaignBudgetListView, CampaignViewSet, PaymentRequestViewSet, TaskViewSet, MyClaimsView, WithdrawalViewSet, WalletView, WalletFundCampaignView, AdminCampaignBudgetViewSet, AdminQuestionViewSet, AdminLevelViewSet, AdminCampaignReviewView, AdminPaymentReviewView, AdminPaymentEvidenceView, AdminWithdrawalReviewView, AdminTaskAnswerReviewView

router = DefaultRouter()
router.register("campaigns", CampaignViewSet, basename="campaign")
router.register("payment-requests", PaymentRequestViewSet, basename="payment-request")
router.register("tasks", TaskViewSet, basename="task")
router.register("withdrawals", WithdrawalViewSet, basename="withdrawal")
router.register("admin/campaign-budgets", AdminCampaignBudgetViewSet, basename="admin-campaign-budget")
router.register("admin/questions", AdminQuestionViewSet, basename="admin-question")
router.register("admin/levels", AdminLevelViewSet, basename="admin-level")

urlpatterns = [
    path("me/", MeView.as_view()),
    path("me/switch-role/", SwitchRoleView.as_view()),
    path("settings/", SettingsView.as_view()),
    path("admin/settings/", AdminSettingsView.as_view()),
    path("levels/", LevelListView.as_view()),
    path("campaign-budgets/", CampaignBudgetListView.as_view()),
    path("my-claims/", MyClaimsView.as_view()),
    path("wallet/", WalletView.as_view()),
    path("wallet/fund-campaign/", WalletFundCampaignView.as_view()),
    path("admin/review/campaigns/", AdminCampaignReviewView.as_view()),
    path("admin/review/campaigns/<int:pk>/", AdminCampaignReviewView.as_view()),
    path("admin/review/payments/", AdminPaymentReviewView.as_view()),
    path("admin/review/payments/<int:pk>/evidence/", AdminPaymentEvidenceView.as_view(), name="admin-payment-evidence"),
    path("admin/review/payments/<int:pk>/", AdminPaymentReviewView.as_view()),
    path("admin/review/withdrawals/", AdminWithdrawalReviewView.as_view()),
    path("admin/review/withdrawals/<int:pk>/", AdminWithdrawalReviewView.as_view()),
    path("admin/review/task-answers/", AdminTaskAnswerReviewView.as_view()),
    path("admin/review/task-answers/<int:pk>/", AdminTaskAnswerReviewView.as_view()),
    path("", include(router.urls)),
]
