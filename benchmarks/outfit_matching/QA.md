# Input quality checks

Source sheets were visually inspected before model scoring. The first distant
generation made subjects too large; a targeted Imagegen correction moved them
farther into the room. Only the corrected `assets/distant.png` is scored. The draft
is retained as generation provenance and is not in the evaluation manifest.

The catalog source is 819×1920 with 4 columns and 5 rows of uneven height. Its
requested landscape panel format was not followed by Imagegen. Manually inspected
panel boundaries are recorded in `panel_layouts.json`; reference crops keep their
portrait aspect ratio instead of stretching them into landscape frames. The low
view sheet is 1619×971 and corrected distant sheet 1620×971, both 5 columns by 4 rows.
Their panels are approximately 324×243 before 2-pixel edge trims.

The 20 outfits remain in their specified row-major order. Visual inspection checked
top type/color, trouser versus short length, trouser color, footwear color, and
horizontal versus vertical stripe orientation across the source sheets. The five
unknown outfits have visible differences from their nearest gallery counterparts:

| Unknown outfit index | Similar reference | Intended visible difference |
|---|---|---|
| 16 | OUTFIT_01 | Black shorts and black footwear instead of blue jeans and white footwear |
| 17 | OUTFIT_04 | Vertical striped button-down instead of horizontal striped shirt |
| 18 | OUTFIT_03 | Gray trousers and white footwear instead of beige trousers and brown footwear |
| 19 | OUTFIT_07 | Blue jeans and white footwear instead of burgundy trousers and black footwear |
| 20 | OUTFIT_08 | Black trousers and black footwear instead of blue jeans and red footwear |

Colors and fabric details drift between generations; this is a benchmark of visible
ensemble equivalence, not exact garment SKU identification. Ground truth is based
on the specified outfits plus manual visual inspection, not solely generation order.
Fine-detail ambiguity is a legitimate reason to return `UNCERTAIN`, recorded
separately. Only one manual inspection was performed; no independent annotation
agreement was measured.

In the first distant 320×240 query, the visible mannequin spans approximately
y=84…159 (about 75 pixels); in the 160×120 query it is about 38 pixels high. This
is a manual example measurement, not a dataset-wide bounding-box annotation.
Low-angle subjects occupy most of their panels. The sheets therefore provide a
clear relative change in image scale but no measured distance in metres.

The poses are mostly frontal and standing. Blank white mannequin heads and hands
are visually salient. The scene does not reproduce realistic facial texture,
natural movement, crowds, or severe furniture occlusion. There is no claim that
the synthetic distribution represents all robot observations.

The harness checks asset hashes before every run. Automated tests verify metric
denominators, errors and abstentions, malformed JSON handling, and that request
construction sends exactly 15 references and one query without query truth labels
or source filenames. Model outcomes were not used to select or discard examples.
