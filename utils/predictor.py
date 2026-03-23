import numpy as np
import os
from tensorflow.keras.models import load_model
from tensorflow.keras.preprocessing import image

# Load model
model = load_model("model/plants_disease_model.h5", compile=false)

# Automatically load class names from dataset folders
dataset_path = "C:/Users/ACER/Desktop/SmartKrishi/PlantVillage/train"

class_names = sorted(os.listdir(dataset_path))


def predict_disease(img_path):

    img = image.load_img(img_path, target_size=(224,224))

    img_array = image.img_to_array(img)/255
    img_array = np.expand_dims(img_array, axis=0)

    prediction = model.predict(img_array)

    index = np.argmax(prediction)

    disease = class_names[index]

    confidence = float(np.max(prediction))*100

    return disease, round(confidence,2)
