from django.apps import AppConfig


class FrameConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "frame"

    def ready(self):
        from . import worker

        if worker.should_start():
            worker.start()
