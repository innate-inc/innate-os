"""Household Orders: find three residents and collect their DoorDash orders."""

from mars_sim_driver.challenges import Challenge, Drop, EventSeen, Goal, SkillDone

from .runtime import HouseholdOrdersRuntime, Resident

RESIDENT_DIALOGUE_RADIUS_M = 2.0

# Stable American-English Cartesia voices recommended for conversational
# agents. Blake's scan is masculine; Alex's and Casey's are feminine.
MASCULINE_VOICE_ID = "a5136bf9-224c-4d76-b823-52bd5efcffcc"  # Jameson
FEMININE_VOICE_ID = "f786b574-daa5-4673-aa0c-cbe3e8534c02"  # Katie

# Orders are deliberately challenge-private: the public roster contains only
# CHALLENGE.brief, while these facts stay inside this package and are disclosed
# by each resident through dialogue. Confirmation checks the order's required
# facts and exclusions, keeping natural paraphrases deterministic while still
# rejecting incomplete or contradictory readbacks.
RESIDENTS = [
    Resident(
        id="alex",
        name="Alex",
        prop="resident_alex",
        order=(
            "From Chipotle, I'd like a chicken burrito bowl with brown rice, black beans, mild salsa, and no cheese."
        ),
        voice_id=FEMININE_VOICE_ID,
        accepted_readbacks=("Chipotle chicken bowl with brown rice, black beans, mild salsa, and without cheese.",),
        required_facts=(
            ("Chipotle",),
            ("chicken burrito bowl", "chicken bowl", "burrito bowl with chicken"),
            ("brown rice",),
            ("black beans",),
            ("mild salsa",),
        ),
        excluded_items=("cheese",),
        radius_m=RESIDENT_DIALOGUE_RADIUS_M,
    ),
    Resident(
        id="blake",
        name="Blake",
        prop="resident_blake",
        order=(
            "From Sweetgreen, I'd like the Harvest Bowl with roasted chicken, "
            "no goat cheese, and the balsamic vinaigrette on the side."
        ),
        voice_id=MASCULINE_VOICE_ID,
        accepted_readbacks=(
            "The Sweetgreen Harvest Bowl with roasted chicken, without goat cheese, and balsamic vinaigrette on the side.",
        ),
        required_facts=(
            ("Sweetgreen", "Sweet Green"),
            ("Harvest Bowl",),
            ("roasted chicken", "with chicken"),
            ("balsamic vinaigrette", "balsamic dressing"),
            ("on the side", "dressing on the side", "vinaigrette on the side"),
        ),
        excluded_items=("goat cheese",),
        radius_m=RESIDENT_DIALOGUE_RADIUS_M,
    ),
    Resident(
        id="casey",
        name="Casey",
        prop="resident_casey",
        order="From Shake Shack, I'd like a ShackBurger with no pickles, cheese fries, and a vanilla shake.",
        voice_id=FEMININE_VOICE_ID,
        accepted_readbacks=("From Shake Shack: a Shack Burger without pickles, cheese fries, and a vanilla shake.",),
        required_facts=(
            ("Shake Shack",),
            ("ShackBurger", "Shack Burger"),
            ("cheese fries", "cheesy fries"),
            ("vanilla shake", "vanilla milkshake"),
        ),
        excluded_items=("pickles",),
        radius_m=RESIDENT_DIALOGUE_RADIUS_M,
    ),
]

CHALLENGE = Challenge(
    id="household_orders",
    title="Household Orders",
    brief=(
        "Three residents are waiting in different rooms. Find each resident, ask for their DoorDash order, "
        "and repeat the full order back until they confirm it. You may visit them in any order. Once all "
        "three orders are confirmed, submit them together with the place_doordash_order skill."
    ),
    # Keep each resident on clear, navigable floor in a different room. These
    # points leave enough clearance for the robot to approach without putting
    # a resident inside bedroom furniture or beyond the apartment walls.
    setup=[
        Drop("resident_alex", -4.59, 4.34, yaw_deg=0),
        Drop("resident_blake", -0.74, -2.76, yaw_deg=180),
        Drop("resident_casey", -0.64, 2.89, yaw_deg=-90),
    ],
    runtime=HouseholdOrdersRuntime(RESIDENTS),
    goals=[
        Goal(
            "Get Alex to confirm the order",
            EventSeen("resident_order_confirmed", {"resident": "alex"}),
            parallel_group="residents",
        ),
        Goal(
            "Get Blake to confirm the order",
            EventSeen("resident_order_confirmed", {"resident": "blake"}),
            parallel_group="residents",
        ),
        Goal(
            "Get Casey to confirm the order",
            EventSeen("resident_order_confirmed", {"resident": "casey"}),
            parallel_group="residents",
        ),
        Goal("Submit all three orders to DoorDash", SkillDone("place_doordash_order")),
    ],
    time_limit_s=900,
)
