from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="frame_index"),
    path("api/state/", views.api_state, name="frame_state"),
    path("api/upload/", views.api_upload, name="frame_upload"),
    path("api/photos/<str:photo_id>/", views.api_photo, name="frame_photo"),
    path("api/stay/", views.api_stay, name="frame_stay"),
    path("api/interval/", views.api_interval, name="frame_interval"),
    path("tvs/", views.tvs_page, name="frame_tvs"),
    path("api/tvs/", views.api_tvs, name="frame_tvs_state"),
    path("api/tvs/scan/", views.api_tvs_scan, name="frame_tvs_scan"),
    path("api/tvs/<int:tv_id>/pair/", views.api_tv_pair, name="frame_tv_pair"),
    path("qr.svg", views.qr, name="frame_qr"),
    path("thumb/<str:photo_id>.jpg", views.thumb, name="frame_thumb"),
    path("g/<str:secret>/", views.guest, name="frame_guest"),
    path("g/<str:secret>/api/state/", views.guest_state, name="frame_guest_state"),
    path("g/<str:secret>/api/upload/", views.guest_upload, name="frame_guest_upload"),
    path("g/<str:secret>/api/interval/", views.guest_interval, name="frame_guest_interval"),
    path("g/<str:secret>/api/photos/<str:photo_id>/", views.guest_photo, name="frame_guest_photo"),
    path("g/<str:secret>/thumb/<str:photo_id>.jpg", views.guest_thumb, name="frame_guest_thumb"),
]
