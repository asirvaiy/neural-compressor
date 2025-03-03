Step-by-Step
============

This example load an object detection model converted from [ONNX Model Zoo](https://github.com/onnx/models) and confirm its accuracy and speed based on [MS COCO 2017 dataset](https://cocodataset.org/#download).

# Prerequisite

## 1. Environment

```shell
pip install neural-compressor
pip install -r requirements.txt
```
> Note: Validated ONNX Runtime [Version](/docs/source/installation_guide.md#validated-software-environment).

## 2. Prepare Model

Download model from [ONNX Model Zoo](https://github.com/onnx/models)

```shell
wget https://github.com/onnx/models/raw/main/vision/object_detection_segmentation/yolov3/model/yolov3-12.onnx
```

## 3. Prepare Dataset

Download [MS COCO 2017 dataset](https://cocodataset.org/#download).

Dataset directories:

```bash
coco2017
├── annotations
|       ├── instances_val2017.json
|       └── ...
├── test2017
├── train2017
└── val2017
```

# Run

## 1. Quantization

Static quantization with QOperator format:

```bash
bash run_tuning.sh --input_model=path/to/model  \ # model path as *.onnx
                   --output_model=path/to/save \ # model path as *.onnx
                   --dataset_location=path/to/coco2017 \ # dataset path containing 'val2017' and 'annotations' folders
                   --label_path=label_map.yaml \
                   --quant_format="QOperator"
```

## 2. Benchmark

```bash
bash run_benchmark.sh --input_model=path/to/model  \ # model path as *.onnx
                      --dataset_location=path/to/coco2017 \ # dataset path containing 'val2017' and 'annotations' folders
                      --label_path=label_map.yaml \
                      --mode=performance # or accuracy
```
