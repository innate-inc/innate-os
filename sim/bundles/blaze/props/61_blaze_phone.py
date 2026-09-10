"""A phone on the study desk -- the graspable replacement for the laptop.

WHY IT EXISTS. blaze_l2 and blaze_l4 asked the robot to carry a laptop out of a
burning house. The laptop's collider is 310 x 220 mm and the gripper's clear
aperture is 61 mm, so no agent could ever complete those challenges; a target
wider than the aperture is now reported ungraspable rather than scored. Scaling
a laptop down to fit would have produced a 50 mm laptop, which is not a laptop.

SIZED FOR A TOP-DOWN GRASP, WHICH IS THE CONSTRAINT THAT MATTERS. pick_any_object
approaches from above, so a flat object has to be gripped across its WIDTH --
its thickness is never the axis the fingers close on. A realistic 150 x 70 mm
phone would therefore fail exactly as the laptop did, just less obviously: 70 mm
against a 61 mm aperture. This is a small phone, 100 x 48 mm, so the grasp axis
is 48 mm with 13 mm of margin inside the aperture.

No mesh: `Prop` leaves a prop as its bare primitive when `mesh` is None, and a
dark slab of these proportions reads as a phone to a vision model without
needing an asset authored for it. The colour is the same slate the laptop used.

drop_z is inherited from the laptop because it is a property of the DESK the
prop is released above, not of the object -- a smaller object on the same desk
is still released from the same height.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="blaze_phone",
    label="?",
    title="Phone",
    collision="box",
    # half-extents: 100 mm long, 48 mm wide, 9 mm thick
    size=(0.050, 0.024, 0.0045),
    rgba=(0.2902, 0.3137, 0.3373, 1.0),
    rest_z=0.0045,
    drop_z=0.257,
)
