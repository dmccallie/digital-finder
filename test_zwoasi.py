import pyzwoasi
from pyzwoasi import ZWOCamera
from pyzwoasi.pyzwoasi import ASIImageType

import cv2
import numpy as np

def stretch_image(image: np.ndarray) -> np.ndarray:
    arr = image.astype(np.float32)
    lo = np.percentile(arr, 5)
    hi = np.percentile(arr, 99.5)
    if hi <= lo:
        hi = lo + 1.0
    scaled = np.clip((arr - lo) / (hi - lo), 0, 1) * 255.0
    return scaled.astype(np.uint8)

numOfConnectedCameras = pyzwoasi.getNumOfConnectedCameras()
if (numOfConnectedCameras == 0):
    print("No camera connected")
    exit()

for cameraIndex in range(numOfConnectedCameras):
    with ZWOCamera(cameraIndex) as camera:
        # set gain
        camera.gain = 0
        # set binning
        camera.softwareBinning = 1


        # get roi
        print(f"Camera {cameraIndex} has ROI: {camera.roi}")

        # cooler status
        print(f"Camera {cameraIndex} has cooler status: {camera.cooler}")

        # note exposure time is in microseconds!
        imageData = camera.shot( exposureTime_us = 1 * 1000000, imageType = ASIImageType.ASI_IMG_RAW16)
        
        # print statistics about the captured image
        min_val = np.min(imageData)
        max_val = np.max(imageData)
        mean_val = np.mean(imageData)
        print(f"Camera {cameraIndex} captured image with min={min_val}, max={max_val}, mean={mean_val}")

        # downscale the image for display to 2K
        displayImage = cv2.resize(imageData, (0, 0), fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        stretchedImage = stretch_image(displayImage)
        
        # show the image using OpenCV
        cv2.imshow(f"Camera {cameraIndex} Image", stretchedImage)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
