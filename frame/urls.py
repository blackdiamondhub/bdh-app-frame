from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="frame_index"),
    path("api/state/", views.api_state, name="frame_state"),
    path("api/upload/", views.api_upload, name="frame_upload"),
    path("api/photos/<str:photo_id>/", views.api_photo, name="frame_photo"),
    path("api/tvs/", views.api_tvs, name="frame_tvs"),
    path("api/stay/", views.api_stay, name="frame_stay"),
    path("qr.svg", views.qr, name="frame_qr"),
    path("thumb/<str:photo_id>.jpg", views.thumb, name="frame_thumb"),
    path("g/<str:secret>/", views.guest, name="frame_guest"),
    path("g/<str:secret>/api/state/", views.guest_state, name="frame_guest_state"),
    path("g/<str:secret>/api/upload/", views.guest_upload, name="frame_guest_upload"),
    path("g/<str:secret>/api/photos/<str:photo_id>/", views.guest_photo, name="frame_guest_photo"),
    path("g/<str:secret>/thumb/<str:photo_id>.jpg", views.guest_thumb, name="frame_guest_thumb"),
]
