import os
import six.moves.urllib as urllib
import sys
import tarfile

from django.conf import settings
import numpy as np
from PIL import Image
import redis
from redis_lock import Lock
import tensorflow as tf

from ..base_model import BaseModel

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from classifiers.object.utils import label_map_util


r = redis.Redis(host=os.environ.get('REDIS_HOST', '127.0.0.1'))
GRAPH_FILE = os.path.join(settings.MODEL_DIR, 'object', 'faster_rcnn_inception_resnet_v2_atrous_lowproposals_oid_2018_01_28_frozen_inference_graph.pb')
LABEL_FILE = os.path.join(settings.MODEL_DIR, 'object', 'oid_bbox_trainable_label_map.pbtxt')


class ObjectModel(BaseModel):
    name = 'object'
    version = 20180124
    approx_ram_mb = 2000

    def __init__(self, graph_file=GRAPH_FILE, label_file=LABEL_FILE):
        super().__init__()
        if self.ensure_downloaded():
            self.graph = self.load_graph(graph_file)
            self.labels = self.load_labels(label_file)

    def load_graph(self, graph_file):
        with Lock(r, 'classifier_{}_load_graph'.format(self.name)):
            if self.name in self.graph_cache:
                return self.graph_cache[self.name]

            graph = tf.Graph()
            graph_def = tf.GraphDef()

            with graph.as_default():
                od_graph_def = tf.GraphDef()
                with tf.gfile.GFile(GRAPH_FILE, 'rb') as fid:
                    serialized_graph = fid.read()
                    od_graph_def.ParseFromString(serialized_graph)
                    tf.import_graph_def(od_graph_def, name='')

            self.graph_cache[self.name] = graph
            return graph

    def load_labels(self, label_file):
        label_map = label_map_util.load_labelmap(label_file)
        categories = label_map_util.convert_label_map_to_categories(label_map, max_num_classes=1000, use_display_name=True)
        return label_map_util.create_category_index(categories)

    def load_image_into_numpy_array(self, image):
        (im_width, im_height) = image.size
        return np.array(image.getdata()).reshape((im_height, im_width, 3)).astype(np.uint8)

    def run_inference_for_single_image(self, image):
        with self.graph.as_default():
            with tf.Session() as sess:
                # Get handles to input and output tensors
                ops = tf.get_default_graph().get_operations()
                all_tensor_names = {output.name for op in ops for output in op.outputs}
                tensor_dict = {}
                for key in [
                    'num_detections', 'detection_boxes', 'detection_scores',
                    'detection_classes', 'detection_masks'
                ]:
                    tensor_name = key + ':0'
                    if tensor_name in all_tensor_names:
                        tensor_dict[key] = tf.get_default_graph().get_tensor_by_name(tensor_name)
                if 'detection_masks' in tensor_dict:
                    # The following processing is only for single image
                    detection_boxes = tf.squeeze(tensor_dict['detection_boxes'], [0])
                    # Reframe is required to translate mask from box coordinates to image coordinates and fit the image size.
                    real_num_detection = tf.cast(tensor_dict['num_detections'][0], tf.int32)
                    detection_boxes = tf.slice(detection_boxes, [0, 0], [real_num_detection, -1])
                image_tensor = tf.get_default_graph().get_tensor_by_name('image_tensor:0')

                # Run inference
                output_dict = sess.run(tensor_dict, feed_dict={image_tensor: np.expand_dims(image, 0)})

                # all outputs are float32 numpy arrays, so convert types as appropriate
                output_dict['num_detections'] = int(output_dict['num_detections'][0])
                output_dict['detection_classes'] = output_dict['detection_classes'][0].astype(np.uint8)
                output_dict['detection_boxes'] = output_dict['detection_boxes'][0]
                output_dict['detection_scores'] = output_dict['detection_scores'][0]
        return output_dict

    def format_output(self, output_dict, min_score):
        results = []
        for i, score in enumerate(output_dict['detection_scores']):
            if score < min_score:
                break

            box = list(output_dict['detection_boxes'][i])
            width = box[3] - box[1]
            height = box[2] - box[0]

            results.append({
                'label':        self.labels[output_dict['detection_classes'][i]]['name'],
                'score':        score,
                'x':            np.mean([box[1], box[3]]),
                'y':            np.mean([box[0], box[2]]),
                'width':        width,
                'height':       height,
                'significance': score * width * height,
                'box':          box,
            })
        return results

    def predict(self, image_file, min_score=0.66):
        image = Image.open(image_file)
        # the array based representation of the image will be used later in order to prepare the
        # result image with boxes and labels on it.
        image_np = self.load_image_into_numpy_array(image)
        # Expand dimensions since the model expects images to have shape: [1, None, None, 3]
        np.expand_dims(image_np, axis=0)
        # Actual detection.
        output_dict = self.run_inference_for_single_image(image_np)
        return self.format_output(output_dict, min_score)


if __name__ == '__main__':
    model = ObjectModel()
    if len(sys.argv) != 2:
        print('Argument required: image file path')
        exit(1)

    results = model.predict(sys.argv[1])

    for result in results:
        print('{} (score: {:0.5f}, significance: {:0.5f}, x: {:0.5f}, y: {:0.5f}, width: {:0.5f}, height: {:0.5f})'.format(result['label'], result['score'], result['significance'], result['x'], result['y'], result['width'], result['height']))