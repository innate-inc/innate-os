from __future__ import print_function
import cv2
import PIL.Image
import numpy as np
import sys
import itertools
from time import time
import datetime
import os
from matplotlib import pyplot as plt
from .contour_detect import  *
from .line_intersection import  *
from .rectify_refine import *

np.set_printoptions(suppress=True, precision=2, linewidth=200)

def detectChessboardCorners(filename):
  """
  Detect chessboard corners and return them for reuse.
  Returns: (corners, success) where corners is a 4x2 numpy array of corner coordinates
  """
  img = cv2.imread(filename)
  if img is None:
    print(f"Error: Could not load image from {filename}")
    return None, False
    
  # img = scaleImageIfNeeded(img, 600, 480)
  img = scaleImageIfNeeded(img, 1024, 768)
  img_orig = img.copy()
  img_orig2 = img.copy()

  # Edges
  edges = cv2.Canny(img, 100, 550)

  # Get mask for where we think chessboard is
  mask, top_two_angles, min_area_rect, median_contour = getEstimatedChessboardMask(img, edges,iters=3) # More iters gives a finer mask
  print("Top two angles (in image coord system): %s" % top_two_angles)

  # Get hough lines of masked edges
  edges_masked = cv2.bitwise_and(edges,edges,mask = (mask > 0.5).astype(np.uint8))
  img_orig = cv2.bitwise_and(img_orig,img_orig,mask = (mask > 0.5).astype(np.uint8))

  lines = getHoughLines(edges_masked, min_line_size=0.25*min(min_area_rect[1]))
  print("Found %d lines." % len(lines))

  lines_a, lines_b = parseHoughLines(lines, top_two_angles, angle_threshold_deg=35)
  
  if len(lines_a) < 2 or len(lines_b) < 2:
    print("Not enough lines found for chessboard detection")
    return None, False

  a = time()
  real_corners = None
  for i2 in range(10):
    for i in range(100):
      corners = chooseRandomGoodQuad(lines_a, lines_b, median_contour)
      
      M = getTileTransform(corners.astype(np.float32),tile_buffer=16, tile_res=16)

      # Warp lines and draw them on warped image
      all_lines = np.vstack([lines_a[:,:2], lines_a[:,2:], lines_b[:,:2], lines_b[:,2:]]).astype(np.float32)
      warp_pts = cv2.perspectiveTransform(all_lines[None,:,:], M)
      warp_pts = warp_pts[0,:,:]
      warp_lines_a = np.hstack([warp_pts[:len(lines_a),:], warp_pts[len(lines_a):2*len(lines_a),:]])
      warp_lines_b = np.hstack([warp_pts[2*len(lines_a):2*len(lines_a)+len(lines_b),:], warp_pts[2*len(lines_a)+len(lines_b):,:]])

      # Get thetas of warped lines 
      thetas_a = np.array([getSegmentTheta(line) for line in warp_lines_a])
      thetas_b = np.array([getSegmentTheta(line) for line in warp_lines_b])
      median_theta_a = (np.median(thetas_a*180/np.pi))
      median_theta_b = (np.median(thetas_b*180/np.pi))
      
      # Gradually relax angle threshold over N iterations
      if i < 20:
        warp_angle_threshold = 0.03
      elif i < 30:
        warp_angle_threshold = 0.1
      elif i < 50:
        warp_angle_threshold = 0.3
      elif i < 70:
        warp_angle_threshold = 0.5
      elif i < 80:
        warp_angle_threshold = 1.0
      else:
        warp_angle_threshold = 2.0
      if ((angleCloseDeg(abs(median_theta_a), 0, warp_angle_threshold) and 
            angleCloseDeg(abs(median_theta_b), 90, warp_angle_threshold)) or 
          (angleCloseDeg(abs(median_theta_a), 90, warp_angle_threshold) and 
            angleCloseDeg(abs(median_theta_b), 0, warp_angle_threshold))):
        print('Found good match (%d): %.2f %.2f' % (i, abs(median_theta_a), abs(median_theta_b)))
        break

    warp_img, M = getTileImage(img_orig, corners.astype(np.float32),tile_buffer=16, tile_res=16)

    lines_x, lines_y, step_x, step_y = getWarpCheckerLines(warp_img)
    if len(lines_x) > 0:
      print('Found good chess lines (%d): %s %s' % (i2, lines_x, lines_y))
      
      # Calculate the real corners in original image coordinates
      warp_corners, all_warp_corners = getRectChessCorners(lines_x, lines_y)
      tile_centers = all_warp_corners + np.array([step_x/2.0, step_y/2.0])
      M_inv = np.matrix(np.linalg.inv(M))
      real_corners, all_real_tile_centers = getOrigChessCorners(warp_corners, tile_centers, M_inv)
      
      print(f"Detected chessboard corners: {real_corners}")
      break
  
  print("Ransac corner detection took %.4f seconds." % (time() - a))

  if real_corners is not None:
    return real_corners.astype(np.float32), True
  else:
    print("Failed to detect chessboard corners")
    return None, False

def processFileWithCorners(filename, corners=None):
  """
  Process a chess image using either detected corners or provided corners.
  If corners is None, detects corners. If corners is provided, uses them directly.
  Returns: (img_masked, edges_masked, warp_img, corners_used)
  """
  img = cv2.imread(filename)
  if img is None:
    print(f"Error: Could not load image from {filename}")
    return None, None, None, None
    
  img = scaleImageIfNeeded(img, 1024, 768)
  img_orig = img.copy()
  img_orig2 = img.copy()

  if corners is None:
    # First time - detect corners
    print("Detecting corners for the first time...")
    detected_corners, success = detectChessboardCorners(filename)
    if not success:
      return None, None, None, None
    corners = detected_corners
  else:
    print("Using provided corners...")

  # Use the corners to get the warped image
  warp_img, _ = getTileImageExact(img_orig2, corners, tile_res=64)
  
  # Create edges for visualization
  edges = cv2.Canny(img, 100, 550)
  
  # Create a simple mask based on corners for visualization
  mask = np.zeros(img.shape[:2], dtype=np.uint8)
  cv2.fillPoly(mask, [corners.astype(np.int32)], 1)
  
  edges_masked = cv2.bitwise_and(edges, edges, mask=mask)
  img_masked_full = cv2.bitwise_and(img, img, mask=mask)
  img_masked = cv2.addWeighted(img, 0.2, img_masked_full, 0.8, 0)
  
  # Draw the corners on the image
  cv2.polylines(img_masked, [corners.astype(np.int32)], True, (150, 50, 255), thickness=3)
  
  return img_masked, edges_masked, warp_img, corners

def processFile(filename):
  """
  Original processFile function - kept for backward compatibility
  """
  result = processFileWithCorners(filename, corners=None)
  if result[0] is None:
    return None, None, None
  return result[0], result[1], result[2]


def other():
  # vals = np.array([224, 231, 238, 257, 271, 278, 300, 321, 342, 358, 362, 383, 404, 425, 436, 463, 474])
  # vals_wrong = np.array([ 257., 278., 300., 321., 342., 358., 362., 383., 404.])
  # vals = np.array([206, 222, 239, 256, 268, 273, 286, 290, 307, 324, 341, 345, 357, 373])
  # vals_wrong = np.array([ 226.5, 239., 256., 268., 273., 286., 290., 307., 319.5])
  # vals = np.array([252, 260, 272, 278, 294, 300, 314, 336, 357, 379, 400])
  # vals = np.array([272, 283, 298, 306, 324, 331, 349, 374, 399, 424, 449])
  # vals = np.array([13, 29, 49, 64, 82, 88, 96, 150, 159, 167, 179, 204, 212, 218, 228, 235, 247, 260, 272, 285, 305, 338, 363, 370, 380, 389, 402, 411, 432, 463, 478])
  vals = np.array([67, 93, 100, 111, 122, 140, 147, 158, 172, 184, 209, 219, 228, 237, 249, 273, 298, 317, 324, 344, 349, 356, 374, 400, 414, 426])

  print(vals)
  print(np.diff(vals))
  # sub_arr = np.abs(vals[:,None] - vals)
  # print(sub_arr)

  n_pts = 3
  n = scipy.special.binom(len(vals),n_pts)
  # devs = np.zeros(n)
  # plt.plot(vals_wrong,np.zeros(len(vals_wrong)),'rs')

  a = time()
  best_spacing = getBestEqualSpacing(vals)
  print("iter cost took %.4f seconds for %d combinations." % (time() - a, n))
  print(best_spacing)
  plt.plot(best_spacing,0.05+np.zeros(len(best_spacing)),'gx')
  
  # plt.hist(devs, 50)

  plt.plot(vals,-0.1 + np.zeros(len(vals)),'k.', ms=10)
  plt.show()



def main(filenames):
  output_dir = 'output_images'
  tiles_dir = os.path.join(output_dir, 'tiles')
  if not os.path.exists(output_dir):
    os.makedirs(output_dir)
  if not os.path.exists(tiles_dir):
    os.makedirs(tiles_dir)
  for filename in filenames:
    a = time()
    img_masked, edges_masked, warp_img = processFile(filename)
    print("Full image file process took %.4f seconds." % (time() - a))
    cv2.imshow('img %s' % filename,img_masked)
    if warp_img is not None:
      cv2.imshow('warp %s' % filename, warp_img)

    if warp_img is not None and warp_img.size > 0:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Resize to 512x512
        resized_img = cv2.resize(warp_img, (512, 512))
        
        # Save the 512x512 image
        out_filename_512 = os.path.join(output_dir, f"rectified_512x512_{timestamp}.png")
        cv2.imwrite(out_filename_512, resized_img)
        print(f"Saved 512x512 image to {out_filename_512}")

        # Cut into 64x64 tiles and save
        tile_size = 64
        grid_size = 8
        for r in range(grid_size):
            for c in range(grid_size):
                y_start = r * tile_size
                y_end = y_start + tile_size
                x_start = c * tile_size
                x_end = x_start + tile_size
                tile = resized_img[y_start:y_end, x_start:x_end]
                
                tile_filename = os.path.join(tiles_dir, f"tile_{timestamp}_{r}_{c}.png")
                cv2.imwrite(tile_filename, tile)
        print(f"Saved {grid_size*grid_size} {tile_size}x{tile_size} tiles.")

    out_filename = filename[:-4].replace('/','_').replace('\\','_')
    print(filename[:-4], out_filename)
    if warp_img is not None:
      PIL.Image.fromarray(cv2.cvtColor(warp_img,cv2.COLOR_BGR2RGB)).save("rectified2/%s.png" % out_filename)
    # cv2.imshow('edges %s' % filename, edges_masked)


  cv2.waitKey(0)
  cv2.destroyAllWindows()
  plt.show()

if __name__ == '__main__':
  if len(sys.argv) > 1:
    filenames = sys.argv[1:]
  else:
    # filenames = ['input2/02.jpg']
    # filenames = ['input2/01.jpg']
    filenames = ['input/30.jpg']
  print("Loading", filenames)
  main(filenames)
  # other()