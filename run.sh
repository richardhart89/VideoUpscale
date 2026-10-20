#!/bin/sh
set -eu

export REALESRGAN_NCNN_VULKAN_PATH="/Users/richardhart/realesrgan-ncnn-vulkan-v0.2.0-macos/realesrgan-ncnn-vulkan"
export REALESRGAN_MODELS="/Users/richardhart/realesrgan-ncnn-vulkan-v0.2.0-macos/models"

cd /Users/richardhart/VideoUpscale
exec /opt/anaconda3/bin/python3 upscale.py --port 8767
