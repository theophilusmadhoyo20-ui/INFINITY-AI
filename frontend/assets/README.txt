INFINITY.100 AI — frontend/assets
=================================

hero.png  (optional)
--------------------
Drop the shared robot reference image here as exactly:

    D:\INFINITY100-AI\frontend\assets\hero.png

On load the UI probes that file. If it decodes, <html> gets data-hero="photo"
and the photo becomes the backdrop. If it is missing or broken, the UI keeps
data-hero="procedural" and draws the inline SVG hero instead — angular white
helmet, red ring eyes, black hooded mantle over a pale grey studio gradient.

Either way the composition is the same: figure centre, roster left, live stats
right, glass panels floating over it. Nothing breaks when the file is absent.

Recommended: PNG or JPG renamed to .png, portrait, subject centred with room
at the bottom, at least 1200x1600. It is rendered `center bottom / cover`.

No other asset is required. There are no fonts, scripts or stylesheets fetched
from the internet — the whole UI runs offline from this folder.
