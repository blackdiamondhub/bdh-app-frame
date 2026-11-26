from bdh_sdk import PluginSpec, Setting, Tile
from bdh_sdk.plugins import BOOL, CHOICE, LIFECYCLE_ALWAYS, MULTI, NETWORK_LAN

MATTES = (
    ("none", "No matte"),
    ("modern_polar", "Modern, white"),
    ("modern_black", "Modern, black"),
    ("shadowbox_polar", "Shadowbox, white"),
    ("shadowbox_black", "Shadowbox, black"),
    ("flexible_polar", "Flexible, white"),
)

spec = PluginSpec(
    id="frame",
    app="frame",
    urls="frame.urls",
    status="frame.homepage.status",
    tiles=(
        Tile("Frame TV", "frame_index", "frame/hub/icon.svg", section="lodge",
             subtitle="Your photos on the Frame TVs", order=40),
    ),
    settings=(
        # The Frame's own slideshow only offers these intervals.
        Setting("interval", "Change photo every", type=CHOICE, default="15",
                choices=(("3", "3 minutes"), ("15", "15 minutes"), ("60", "1 hour"),
                         ("720", "12 hours"), ("1440", "1 day"))),
        Setting("shuffle", "Shuffle", type=BOOL, default=True),
        Setting("matte", "Matte for new photos", type=CHOICE, default="none", choices=MATTES),
        Setting("guest_uploads", "Guests can add photos during their stay", type=BOOL, default=True,
                help="Family photos come off the TVs during guest stays either way."),
        Setting("skip_tvs", "Leave these TVs alone", type=MULTI, default=None,
                choices_from="frame.discovery.tv_choices",
                help="Frame uses every Frame TV it finds on the network (it looks every 10 minutes; "
                     "the TV must be on). A new TV asks you to press Allow on it. Tick a TV to take "
                     "Frame's photos off it and leave it alone."),
        Setting("scan_subnets", "Also look for TVs on", default="",
                help="Other subnets with Frame TVs, e.g. 192.168.11.0/24 (the hub's own network "
                     "is always searched)."),
    ),
    api_version=2,
    # Who is staying decides whether family or guest photos are on the TVs.
    permissions=("stays.read",),
    # The TVs are on the LAN (one is on another VLAN, reached through a UDM SNAT rule).
    network=NETWORK_LAN,
    # Keeps the TVs in step with the photos and ends guest stays at midnight.
    lifecycle=LIFECYCLE_ALWAYS,
    # Pillow holds a decoded 4K photo (~25 MB) while resizing an upload.
    memory_mb=192,
)
