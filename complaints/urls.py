from django.urls import path

from . import views

app_name = "complaints"

urlpatterns = [
    path("", views.complaint_list, name="list"),
    path("c/<int:pk>/", views.complaint_detail, name="detail"),
    path("c/<int:pk>/do/<str:action>/", views.complaint_action, name="action"),
    path("documents/<int:pk>/", views.document_download, name="document"),
    path("new/", views.intake_upload, name="intake_upload"),
    path("new/review/", views.intake_review, name="intake_review"),
    path("overdue/", views.overdue_today, name="overdue"),
]
