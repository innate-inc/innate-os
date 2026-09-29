# One remembered frame the vision model says a current view shows.
int32 frame          # spatial-memory id
int32 view           # index into the goal's images
float64 confidence   # 0..1
float64 x            # map pose the frame was recorded from
float64 y
float64 theta
string evidence      # what both images share
