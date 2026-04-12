from typing import List

import cv2
import matplotlib.pyplot as plt
import matplotlib.cm as cm

import datatypes
    
def visualize_waypoints(image: cv2.Mat, waypoints: List[List[datatypes.Waypoint]]):
    plt.imshow(image, cmap='gray')
    cmap = cm.get_cmap('Reds')
    n = len(waypoints)
    for i, path in enumerate(waypoints):
        xs = [wp.x for wp in path]
        ys = [wp.y for wp in path]
        color = cmap((i + 1) / n)
        plt.plot(xs, ys, color=color)
    plt.show()

def visualize_actions(image: cv2.Mat, actions: List[datatypes.Action]):
    plt.imshow(image, cmap='gray')
    n = len(actions)
    cmap = cm.get_cmap('Reds')
    n = len(actions)

    for i, action in enumerate(actions):
        if action.type == datatypes.ActionType.WAYPOINT:
            xs = [action.waypoint.x]
            ys = [action.waypoint.y]
            color = cmap((i + 1) / n)
            plt.plot(xs, ys, 'o', color=color)

    plt.show()
