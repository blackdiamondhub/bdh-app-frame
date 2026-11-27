# bdh-app-frame

Black Diamond Hub app for Samsung Frame TVs. It puts photos on the Frames' art mode and runs
their slideshow, talking to the TVs directly over the LAN (samsungtvws, no Home Assistant).

## How it works

- **Family photos** are uploaded by hub admins (from the hub screen, or from a phone with the
  "Add from phone" QR, which is valid for 30 minutes). They are shown when no guests are staying.
- **Guest stays** come from the hub (`stays.read`). When a guest stay starts, family photos are
  removed from every TV and are not visible anywhere in the app except to admins.
- **Guests** open the app on the hub screen or scan its QR code to upload from their phones. Any
  guest in the group can add or remove any of the group's photos and pick which TVs show them.
- **After checkout**, at midnight (lodge time) after the checkout date, the stay's photos are
  permanently deleted from the hub and the TVs, and the guest link stops working. New guests
  arriving or owners arriving end the stay straight away, and admins can end it early.
- Uploads can be JPEG, PNG or HEIC. The upload pages accept HEIC so that iPhones send their
  originals instead of converting each photo to JPEG before the picker closes, which is slow.
- Photos are resized to 4K JPEG on the hub before they go to a TV. Photos already on a TV that
  Frame didn't put there are left alone (shown as "unmanaged" on the admin page).

## Adding a TV

Admin page → **Add TV** → the TV's IP. The TV shows an "Allow" prompt for "BDH Frame" the first
time; accept it with the remote. The TV must be on and reachable from the hub (ports 8001/8002).

## Settings

| Key | Type | |
| --- | --- | --- |
| `interval` | choice | Minutes per photo in the slideshow |
| `shuffle` | bool | Shuffle the slideshow |
| `guest_uploads` | bool | Let guests add photos |

Photos are sent to the TVs as 3840×2160 with the whole photo in the middle and a blurred copy
filling the sides, never with a TV matte: Frame TVs crop non-16:9 photos, and their art app
crashes (error 40000) on a matted non-16:9 photo.

## Network

The app needs `lan` (install with `--allow-lan`). Guest phones reach the app's port on the hub,
so the guest network needs a firewall allow to the hub's IP on that port.
