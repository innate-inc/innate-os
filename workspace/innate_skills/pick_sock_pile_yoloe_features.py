"""YOLO-E pile pickup excluding a feature-localized basket and its surroundings."""
from innate import SkillReturn
from innate_skills.pick_sock_pile_yoloe import PickSockPileYoloe
from innate_skills.pick_sock_yoloe import PickSockYoloe
from innate_skills.feature_basket_exclusion import FeatureBasketExclusion


class PickSockPileYoloeFeatures(PickSockPileYoloe):
    """Pick floor socks outside the feature-detected basket and a 10 cm margin.

    Excludes sock boxes overlapping the basket, including above its rim.
    Searches left when all candidates are excluded. Keep the basket stationary.
    """
    def execute(self) -> SkillReturn:
        self._box_exclusion = FeatureBasketExclusion(self)
        try:
            return PickSockYoloe.execute(self)
        finally:
            self._box_exclusion.close()
