"""Run."""

import os
import sys
import gc

import argparse
import glob

from typing import List, Tuple

import numpy as np
import tensorflow as tf

import matplotlib.image as mpimg
import matplotlib.pyplot as plt

from dfp.data import convert_one_hot_to_image
from dfp.net import deepfloorplanModel
from dfp.utils.rgb_ind_convertor import (
    floorplan_boundary_map,
    floorplan_fuse_map,
    ind2rgb,
)
from dfp.utils.util import fill_break_line, flood_fill, refine_room_region

os.environ["TF_FORCE_GPU_ALLOW_GROWTH"] = "true"


IMG_TYPES = ['gif', 'jpeg', 'jpg', 'png']


def init_model(config: argparse.Namespace) -> tf.keras.Model:
    """Init model."""
    model = deepfloorplanModel()
    if config.loadmethod == "log":
        model.load_weights(config.weight)
        model.trainable = False
        model.vgg16.trainable = False
    elif config.loadmethod == "pb":
        model = tf.keras.models.load_model(config.weight)
        model.trainable = False
        model.vgg16.trainable = False
    elif config.loadmethod == "tflite":
        model = tf.lite.Interpreter(model_path=config.weight)
        model.allocate_tensors()
    return model


def init_image(img_path: str) -> Tuple[tf.Tensor, np.ndarray]:
    """Init image."""
    img = mpimg.imread(img_path)
    shp = img.shape
    print(img_path, ':', shp)
    img = tf.convert_to_tensor(img, dtype=tf.uint8)
    if len(shp) == 2:
        img = tf.expand_dims(img, -1)
        img = tf.image.grayscale_to_rgb(img)
        shp = np.array((shp[0], shp[1], 3))
    img = tf.image.resize(img, [512, 512])
    img = tf.cast(img, dtype=tf.float32)
    img = tf.reshape(img, [-1, 512, 512, 3]) / 255
    return img, shp


def predict(
    model: tf.keras.Model, img: tf.Tensor, shp: np.ndarray
) -> Tuple[tf.Tensor, tf.Tensor]:
    """Predict."""
    features = []
    feature = img
    for layer in model.vgg16.layers:
        feature = layer(feature)
        if layer.name.find("pool") != -1:
            features.append(feature)
    x = feature
    features = features[::-1]
    del model.vgg16
    gc.collect()

    featuresrbp = []
    for i in range(len(model.rbpups)):
        x = model.rbpups[i](x) + model.rbpcv1[i](features[i + 1])
        x = model.rbpcv2[i](x)
        featuresrbp.append(x)
    logits_cw = tf.keras.backend.resize_images(
        model.rbpfinal(x), 2, 2, "channels_last"
    )

    x = features.pop(0)
    nLays = len(model.rtpups)
    for i in range(nLays):
        rs = model.rtpups.pop(0)
        r1 = model.rtpcv1.pop(0)
        r2 = model.rtpcv2.pop(0)
        f = features.pop(0)
        x = rs(x) + r1(f)
        x = r2(x)
        a = featuresrbp.pop(0)
        x = model.non_local_context(a, x, i)

    del featuresrbp
    logits_r = tf.keras.backend.resize_images(
        model.rtpfinal(x), 2, 2, "channels_last"
    )
    del model.rtpfinal

    return logits_cw, logits_r


def post_process(
    rm_ind: np.ndarray, bd_ind: np.ndarray, shp: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Post process."""
    hard_c = (bd_ind > 0).astype(np.uint8)
    # region from room prediction
    rm_mask = np.zeros(rm_ind.shape)
    rm_mask[rm_ind > 0] = 1
    # region from close wall line
    cw_mask = hard_c
    # regine close wall mask by filling the gap between bright line
    cw_mask = fill_break_line(cw_mask)
    cw_mask = np.reshape(cw_mask, (*shp[:2], -1))
    fuse_mask = cw_mask + rm_mask
    fuse_mask[fuse_mask >= 1] = 255

    # refine fuse mask by filling the hole
    fuse_mask = flood_fill(fuse_mask)
    fuse_mask = fuse_mask // 255

    # one room one label
    new_rm_ind = refine_room_region(cw_mask, rm_ind)

    # ignore the background mislabeling
    new_rm_ind = fuse_mask.reshape(*shp[:2], -1) * new_rm_ind
    new_bd_ind = fill_break_line(bd_ind).squeeze()
    return new_rm_ind, new_bd_ind


def colorize(r: np.ndarray, cw: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Colorize."""
    cr = ind2rgb(r, color_map=floorplan_fuse_map)
    ccw = ind2rgb(cw, color_map=floorplan_boundary_map)
    return cr, ccw


def run_on_one(config: argparse.Namespace, model, img, shp) -> np.ndarray:
    """Run."""
    if config.loadmethod == "log":
        logits_cw, logits_r = predict(model, img, shp)
    elif config.loadmethod == "pb":
        logits_r, logits_cw = model(img)
    elif config.loadmethod == "tflite":
        input_details = model.get_input_details()
        output_details = model.get_output_details()
        model.set_tensor(input_details[0]["index"], img)
        model.invoke()
        logits_r = model.get_tensor(output_details[0]["index"])
        logits_cw = model.get_tensor(output_details[1]["index"])
        logits_cw = tf.convert_to_tensor(logits_cw)
        logits_r = tf.convert_to_tensor(logits_r)
    logits_r = tf.image.resize(logits_r, shp[:2])
    logits_cw = tf.image.resize(logits_cw, shp[:2])
    r = convert_one_hot_to_image(logits_r)[0].numpy()
    cw = convert_one_hot_to_image(logits_cw)[0].numpy()

    if not config.colorize and not config.postprocess:
        cw[cw == 1] = 9
        cw[cw == 2] = 10
        r[cw != 0] = 0
        return (r + cw).squeeze()
    elif config.colorize and not config.postprocess:
        r_color, cw_color = colorize(r.squeeze(), cw.squeeze())
        return r_color + cw_color

    newr, newcw = post_process(r, cw, shp)
    if not config.colorize and config.postprocess:
        newcw[newcw == 1] = 9
        newcw[newcw == 2] = 10
        newr[newcw != 0] = 0
        return newr.squeeze() + newcw
    newr_color, newcw_color = colorize(newr.squeeze(), newcw.squeeze())
    result = newr_color + newcw_color

    return result


def parse_args(args: List[str]) -> argparse.Namespace:
    """Parse args."""
    p = argparse.ArgumentParser()
    p.add_argument("--images", type=str)
    p.add_argument("--image", type=str, default="resources/30939153.jpg")
    p.add_argument("--weight", type=str, default="log/store/G")
    p.add_argument("--postprocess", action="store_true")
    p.add_argument("--colorize", action="store_true")
    p.add_argument(
        "--loadmethod",
        type=str,
        default="log",
        choices=["log", "tflite", "pb"],
    )
    return p.parse_args(args)


def save_result(img_path: str, result: np.ndarray):
    """Save result."""
    save_path = f"{img_path.split('.')[0]}.jpg"
    save_dir = os.path.dirname(save_path)
    os.makedirs(f"out/{save_dir}", exist_ok=True)
    mpimg.imsave(f"out/{save_path}", result.astype(np.uint8))


def plot_result(result: np.ndarray):
    """Plot result."""
    plt.imshow(result)
    plt.xticks([])
    plt.yticks([])
    plt.grid(False)


def main():
    """Run main."""
    args = parse_args(sys.argv[1:])

    model = init_model(args)

    if args.images:
        imgs = []
        for itype in IMG_TYPES:
            imgs_type = glob.glob(f"{args.images}/**/*.{itype}")
            imgs.extend(imgs_type)
        imgs = sorted(imgs)
    else:
        imgs = [args.image]

    for ipath in imgs:
        img, shp = init_image(ipath)
        result = run_on_one(args, model, img, shp)
        print("Result:", result.shape)
        if args.images:
            ipath = ipath.replace(args.images, '')
        else:
            ipath = os.path.basename(args.image)
        save_result(ipath, result)
        # plot_result(result)

    # plt.show()


if __name__ == "__main__":
    main()
