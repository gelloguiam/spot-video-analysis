from re import X
import cv2
import core.utils as utils
import matplotlib.pyplot as plt
import math
import numpy as np
import tensorflow as tf
import time

from absl import app, flags, logging
from absl.flags import FLAGS
from core.config import cfg
from core.yolov4 import filter_boxes
from PIL import Image
from tensorflow.compat.v1 import ConfigProto
from tensorflow.compat.v1 import InteractiveSession
from tensorflow.python.saved_model import tag_constants
import spot_library as spot

from deep_sort import preprocessing, nn_matching
from deep_sort.detection import Detection
from deep_sort.tracker import Tracker
from tools import generate_detections as gdet


flags.DEFINE_string('weights', './checkpoints/yolov4-416', 'path to weights file')
flags.DEFINE_integer('size', 416, 'resize images to')
flags.DEFINE_boolean('tiny', False, 'yolo or yolo-tiny')
flags.DEFINE_string('model', 'yolov4', 'yolov3 or yolov4')
flags.DEFINE_float('iou', 0.20, 'iou threshold')
flags.DEFINE_float('score', 0.10, 'score threshold')
flags.DEFINE_string('video', './data/video/sample-org.mov', 'path to input video or set to 0 for webcam')
flags.DEFINE_string('output', 'output/output.mp4', 'path to output video')


def main(_argv):
    # Definition of the parameters
    max_cosine_distance = 0.4
    nn_budget = None
    nms_max_overlap = 5.0
    
    # initialize deep sort
    model_filename = 'model_data/mars-small128.pb'
    encoder = gdet.create_box_encoder(model_filename, batch_size=1)

    # calculate cosine distance metric
    metric = nn_matching.NearestNeighborDistanceMetric("cosine", max_cosine_distance, nn_budget)

    # initialize tracker
    tracker = Tracker(metric)

    # load configuration for object detector
    config = ConfigProto()
    config.gpu_options.allow_growth = True
    session = InteractiveSession(config=config)
    STRIDES, ANCHORS, NUM_CLASS, XYSCALE = utils.load_config(FLAGS)
    input_size = FLAGS.size
    video_path = FLAGS.video

    saved_model_loaded = tf.saved_model.load(FLAGS.weights, tags=[tag_constants.SERVING])
    infer = saved_model_loaded.signatures['serving_default']

    # begin video capture
    try:
        vid = cv2.VideoCapture(int(video_path))
    except:
        vid = cv2.VideoCapture(video_path)

    #define video writer to output video
    video_writer = None
    width = int(vid.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = int(vid.get(cv2.CAP_PROP_FPS))
    codec = cv2.VideoWriter_fourcc('M', 'J', 'P', 'G')
    video_writer = cv2.VideoWriter(FLAGS.output, codec, fps, (width, height))

    frame_num = 0

    while True:
        return_value, frame = vid.read()
        if return_value:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(frame)
        else:
            print('Video has ended or failed, try a different video format!')
            break

        frame_num += 1
        # if frame_num < 1200: continue

        print('Frame #: ', frame_num)
        image_data = cv2.resize(frame, (input_size, input_size))
        image_data = image_data / 255.
        image_data = image_data[np.newaxis, ...].astype(np.float32)

        batch_data = tf.constant(image_data)
        pred_bbox = infer(batch_data)
        for key, value in pred_bbox.items():
            boxes = value[:, :, 0:4]
            pred_conf = value[:, :, 4:]

        boxes, scores, classes, valid_detections = tf.image.combined_non_max_suppression(
            boxes=tf.reshape(boxes, (tf.shape(boxes)[0], -1, 1, 4)),
            scores=tf.reshape(
                pred_conf, (tf.shape(pred_conf)[0], -1, tf.shape(pred_conf)[-1])),
            max_output_size_per_class=50,
            max_total_size=50,
            iou_threshold=FLAGS.iou,
            score_threshold=FLAGS.score
        )

        # convert data to numpy arrays and slice out unused elements
        num_objects = valid_detections.numpy()[0]
        bboxes = boxes.numpy()[0]
        bboxes = bboxes[0:int(num_objects)]
        scores = scores.numpy()[0]
        scores = scores[0:int(num_objects)]
        classes = classes.numpy()[0]
        classes = classes[0:int(num_objects)]

        # format bounding boxes from normalized ymin, xmin, ymax, xmax ---> xmin, ymin, width, height
        original_h, original_w, _ = frame.shape
        bboxes = utils.format_boxes(bboxes, original_h, original_w)

        # store all predictions in one parameter for simplicity when calling functions
        pred_bbox = [bboxes, scores, classes, num_objects]

        # print("all objects: {} ".format(num_objects))

        # read in all class names from config
        class_names = utils.read_class_names(cfg.YOLO.CLASSES)

        # by default allow all classes in .names file
        # allowed_classes = list(class_names.values())        
        allowed_classes = ['person']

        # loop through objects and use class index to get class name, allow only classes in allowed_classes list
        names = []
        deleted_indx = []
        for i in range(num_objects):
            class_indx = int(classes[i])
            class_name = class_names[class_indx]
            if class_name not in allowed_classes:
                deleted_indx.append(i)
            else:
                names.append(class_name)
        names = np.array(names)
        count = len(names)

        # delete detections that are not in allowed_classes
        bboxes = np.delete(bboxes, deleted_indx, axis=0)
        scores = np.delete(scores, deleted_indx, axis=0)

        count = len(names)
        cv2.putText(frame, "Tracking {} people on frame {}".format(count, frame_num), (10, 50), cv2.FONT_HERSHEY_PLAIN, 1, (0, 255, 0), 1)

        # encode yolo detections and feed to tracker
        features = encoder(frame, bboxes)
        detections = [Detection(bbox, score, class_name, feature) for bbox, score, class_name, feature in zip(bboxes, scores, names, features)]

        # initialize color map
        cmap = plt.get_cmap('tab20b')
        colors = [cmap(i)[:3] for i in np.linspace(0, 1, 20)]

        # run non-maxima supression
        boxs = np.array([d.tlwh for d in detections])
        scores = np.array([d.confidence for d in detections])
        classes = np.array([d.class_name for d in detections])
        indices = preprocessing.non_max_suppression(boxs, classes, nms_max_overlap, scores)
        detections = [detections[i] for i in indices]

        # Call the tracker
        tracker.predict()
        tracker.update(detections)

        # update tracks
        for track in tracker.tracks:
            if not track.is_confirmed() or track.time_since_update > 1:
                continue 
            bbox = track.to_tlbr()
            class_name = track.get_class()

            x1 = int(bbox[0])
            x2 = int(bbox[2])
            y1 = int(bbox[1])
            y2 = int(bbox[3])

            area = (x2-x1)*(y2-y1)
            if area > 100000: continue

            # get centroid of the bounding box
            cX = int((x1 + x2)/2)
            cY = int((y1 + y2)/2)
            
            curr_centroid = (cX, cY)
            prev_centroid_1 = track.get_last_centroid(1)
            prev_centroid_2 = track.get_last_centroid(2)

            angle_1 = 500
            angle_2 = 500

            if prev_centroid_1 is not None and prev_centroid_2 is not None:
                distance = spot.get_distance(curr_centroid, prev_centroid_1)
                if(distance > 1 and distance < 10):

                    angle_1 = spot.get_angle(prev_centroid_1, curr_centroid)
                    angle_2 = spot.get_angle(prev_centroid_2, prev_centroid_1)

                    if(angle_1 == 0 or angle_2 == 0):
                        angle_1 = 0
                        angle_2 = 0

                    # if abs(angle_1-angle_2) > 10:
                    #     print(track.track_id, "distance", distance, angle_1, angle_2)

            track.update_centroid(cX, cY)
            angle_diff = abs(angle_1-angle_2)
            angle_diff = round(angle_diff, 2)
            angle_1 = round(angle_1, 2)

            # if(track.track_id == 21):
            #     print(angle_diff)

        # draw bbox on screen
            color = colors[int(track.track_id) % len(colors)]
            color = [i * 255 for i in color]

            # if angle_diff > 50:
            # # if prev_centroid_1 is not None and prev_centroid_2 is not None:
            #     cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 5)
            #     print(track.track_id, angle_1, angle_2, angle_diff)

                # cv2.rectangle(frame, (x1, y1-30), (x1+(len(str(angle_diff)))*17, y1), color, -1)
                # cv2.putText(frame, str(angle_diff),(x1, y1-10), 0 , 0.75, (255,255,255), 2)
                # cv2.line(frame, curr_centroid, prev_centroid_1,color,3)
                # cv2.line(frame, prev_centroid_1, prev_centroid_2,color,3)
            # cv2.putText(frame, str(track.track_id),(x1, y1-10), 0 , 0.75, color, 2)

            all_centroids = track.centroids
            for cent in all_centroids:
                cv2.circle(frame, cent, radius=0, color=color, thickness=5)

            # cv2.putText(frame, str(curr_centroid),(x1, y1-10), 0 , 0.75, color, 2)

            # else:
            # cv2.rectangle(frame, (x1, y1), (x2, y2), color, 1)
            #     cv2.rectangle(frame, (x1, y1-30), (x1+(len(str(angle_1)))*17, y1), color, -1)
            #     cv2.putText(frame, str(angle_1),(x1, y1-10), 0 , 0.75, (255,255,255), 2)

            # cv2.putText(frame, str(track.track_id),(x1, y1-25), 0 , 0.75, (0,255,0), 2)

            # cv2.rectangle(frame, (x1, y1-30), (x1+(len(str(track.track_id)))*17, y1), color, -1)
            # cv2.putText(frame, str(track.track_id),(x1, y1-10), 0 , 0.75, (255,255,255), 2)

            # cv2.circle(frame, (cX, cY), radius=0, color=color, thickness=20)

            # print("Tracker ID: {}, Class: {},  BBox Coords (xmin, ymin, xmax, ymax): {}".format(str(track.track_id), class_name, (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))))

        result = np.asarray(frame)
        result = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        
        cv2.imshow("Output Video", result)

        # write the result
        video_writer.write(result)

        # ESC to end video
        c = cv2.waitKey(1) % 0x100
        if c == 27: break

    cv2.destroyAllWindows()


if __name__ == '__main__':
    try:
        app.run(main)
    except SystemExit:
        pass
