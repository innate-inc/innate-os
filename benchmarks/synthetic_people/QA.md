# Dataset QA before scoring

All scored subjects are fictional characters generated for this task. No real-person photographs were used as references, and no pixels from the supplied robot video were included.

The four source sheets and the corrected face-query sheet were visually inspected before API scoring. Source images are 1122×1402. The sheets did not exactly honor the requested equal panel sizes, so the measured panel boundaries are stored explicitly in the manifest. A two-pixel inset removes panel boundaries. Full-scene images are resized to 320×240 or 160×120; source panels have slightly differing aspect ratios.

The first face-query generation put the final column's characters in the wrong order. It was retained as `face_queries_draft.png` and never scored. An image-generation correction replaced the final column using the fourth original sheet. The corrected order was inspected before cropping and scoring. No recognition scores were used to select, regenerate or relabel images.

Face boxes for preparation were found with YuNet on source-resolution synthetic portraits, requiring exactly one hit per portrait. Square crops center on the detected face and use 1.04 times its longest box dimension. The crops are unaligned and unenhanced. Each crop's coordinates, detector box, locator score and original dimensions are in `annotations.json`. All reference and face-query crop pairs were inspected on `assets/qa_faces.jpg` before scoring. Native face-query crops range from 154–201 px; all query sizes are downsampled, never upscaled from source.

The cropped face-only inputs omit clothing, but retain parts of hair, facial hair and face outline. The changed perspective is visibly upward in the face-only queries. Full-body scene clothing is shared by all characters and changes from gray in enrollment to blue in query views. Backgrounds are not perfectly identical across generator batches, and full-scene views retain body shape and hairstyle. The model-visible reference crops contain no office background.

Held-out indices are 4, 9, 14, 19, 20. Several were designed as lookalikes of enrolled characters. Visual inspection reveals very close appearance in some pairs, and the generator may have converged on similar identities. Their intended identities are specified by the generation prompts; there is no independent real-person identity ground truth. Preserve this ambiguity in interpretation of unknown-rejection scores. Do not claim a real-world false-acceptance rate.

Ground-truth labels and query filenames are withheld from requests. Model-visible names were assigned to the 15 fictional references after generation. Each evaluated query has a shuffled reference order fixed in the manifest; both API providers use the identical order. The unknown enrollment images and QA sheet are never part of the gallery.

The dataset was frozen before model predictions. A later code-formatting pass did not alter function/class ASTs. Exact executed scripts are archived in `executed_sources/`; the before/after hashes and verification are recorded in `source_history.json`. The run records refer to the archived executed hashes. Imported modules and assets are shared with the previous outfit benchmark only where explicitly recorded; original outfit results remain separate.
