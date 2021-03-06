import math

import tensorflow as tf
import tensorflow.keras as keras
from tensorflow.keras.applications.vgg16 import VGG16

from config import Config as c


class Sampling(keras.layers.Layer):
    """Uses (z_mean, z_logvar) to sample z the vector encoding a patch"""

    def call(self, inputs):
        z_mean, z_log_var = inputs
        batch = tf.shape(z_mean)[0]
        dim = tf.shape(z_mean)[1]
        a = tf.exp(0.5 * z_log_var)
        # Casting only required when using mixed_precision
        # Without it, it results in a float16 and float32 type mismatch
        epsilon = tf.cast(tf.keras.backend.random_normal(shape=(batch, dim)), dtype=a.dtype)
        return z_mean + a * epsilon


class ConvolutionalVAE(keras.Model):
    def __init__(self, gamma=100, **kwargs):
        super(ConvolutionalVAE, self).__init__(**kwargs)
        self.code_dim_size = c.latent_dim
        self.target_image_dims = c.input_shape
        # Normalized beta value
        self.b_norm = gamma * c.b_norm * self.code_dim_size / math.prod(self.target_image_dims)
        print(f'Beta Normalized value: {self.b_norm}')
        e_input = keras.Input(shape=self.target_image_dims)  # (136, 64, 1)
        for i, filter in enumerate(c.filters):
            if i == 0:
                x = self._conv_block(e_input, filter)
            else:
                x = self._conv_block(x, filter)

        x = keras.layers.Flatten()(x)
        z_mean_dense = keras.layers.Dense(self.code_dim_size, name='z_mean')(x)

        z_logvar_dense = keras.layers.Dense(self.code_dim_size, name='z_logvar')(x)

        z = Sampling()([z_mean_dense, z_logvar_dense])
        self.encoder = keras.Model(e_input, [z_mean_dense, z_mean_dense, z], name='encoder')
        self.encoder.summary()

        latent_input = keras.Input(shape=(self.code_dim_size,))
        d = keras.layers.Dense(c.last_conv_dim * c.last_conv_dim * c.filters[-1], activation='relu')(latent_input)
        d = keras.layers.Reshape((c.last_conv_dim, c.last_conv_dim, c.filters[-1]))(d)

        for i, filter in enumerate(reversed(c.filters)):
            d = self._deconv_block(d, filter, i)

        decoder_output = keras.layers.Conv2DTranspose(filters=self.target_image_dims[-1], kernel_size=c.kernels, strides=1, padding='same')(d)
        decoder_output = keras.layers.Activation('sigmoid', dtype='float32')(decoder_output)
        self.decoder = keras.Model(latent_input, decoder_output, name='decoder')
        self.decoder.summary()

        print('Loading VGG Model Weights')
        model = VGG16(include_top=False)
        vgg_conv_block_ixs = [1, 4, 7, 11, 15]
        outputs = [model.layers[i].output for i in vgg_conv_block_ixs]
        self.vgg_model = keras.Model(inputs=model.inputs, outputs=outputs)

        self.total_loss_tracker = keras.metrics.Mean(name='total_loss')
        self.reconstruction_loss_tracker = keras.metrics.Mean(name='reconstruction_loss')
        self.kl_loss_tracker = keras.metrics.Mean(name='kl_loss')
        self.perception_loss_tracker = keras.metrics.Mean(name='p_loss')

    def _conv_block(self, input, filter):
        with tf.name_scope('conv_block'):
            conv = keras.layers.Conv2D(filters=filter, kernel_size=c.kernels, strides=c.strides, padding='same')(input)
            conv = keras.layers.BatchNormalization()(conv)
            outputs = keras.layers.Activation(activation=tf.nn.leaky_relu)(conv)
        return outputs

    def _deconv_block(self, input, filter, i):
        with tf.name_scope('deconv_block'):
            deconv = keras.layers.Conv2DTranspose(filters=filter, kernel_size=c.kernels, strides=c.strides, padding='same')(input)
            deconv = keras.layers.BatchNormalization()(deconv)
            outputs = keras.layers.Activation(activation=tf.nn.leaky_relu)(deconv)
        return outputs

    @property
    def metrics(self):
        return [
            self.total_loss_tracker,
            self.reconstruction_loss_tracker,
            self.kl_loss_tracker,
            self.perception_loss_tracker
        ]

    def call(self, inputs, training=None, mask=None):
        z_mean, z_log_var, z = self.encoder(inputs)
        reconstruction = self.decoder(z)
        return reconstruction

    def _total_loss_fn(self, reconstruction_loss, kl_loss, perception_loss):
        reconstruction_loss = 0.5 * reconstruction_loss
        return reconstruction_loss + 0.5 * perception_loss + kl_loss

    def test_step(self, data):
        z_mean, z_log_var, z = self.encoder(data)
        reconstruction = self.decoder(z)
        feature_maps_data = self.vgg_model(data)
        feature_maps_reconstruction = self.vgg_model(reconstruction)
        feature_losses = []
        for i in range(len(feature_maps_data)):
            feature_map_data = keras.layers.Flatten()(feature_maps_data[i])
            feature_map_reconstruction = keras.layers.Flatten()(feature_maps_reconstruction[i])
            feature_losses.append(tf.reduce_mean(tf.reduce_sum(tf.pow(feature_map_data - feature_map_reconstruction, 2))))

        perception_loss = tf.math.add_n(feature_losses, 'perception_loss')

        reconstruction_loss = tf.reduce_mean(tf.reduce_sum(keras.losses.binary_crossentropy(data, reconstruction), axis=(1, 2)))
        kl_loss = -0.5 * (1 + z_log_var - tf.square(z_mean) - tf.exp(z_log_var))
        kl_loss = tf.reduce_mean(tf.reduce_sum(kl_loss, axis=1))
        total_loss = self._total_loss_fn(reconstruction_loss, kl_loss, perception_loss)

        return {
            "loss": total_loss,
            "reconstruction_loss": reconstruction_loss,
            "kl_loss": kl_loss,
            "perception_loss": perception_loss
        }

    def train_step(self, data):
        with tf.GradientTape() as tape:
            # plot_spectrogram(data[0], batched=True)
            z_mean, z_log_var, z = self.encoder(data)
            reconstruction = self.decoder(z)

            feature_maps_data = self.vgg_model(data)
            feature_maps_reconstruction = self.vgg_model(reconstruction)
            feature_losses = []
            for i in range(len(feature_maps_data)):
                feature_map_data = keras.layers.Flatten()(feature_maps_data[i])
                feature_map_reconstruction = keras.layers.Flatten()(feature_maps_reconstruction[i])
                feature_losses.append(tf.reduce_mean(tf.reduce_sum(tf.pow(feature_map_data - feature_map_reconstruction, 2))))

            perception_loss = tf.math.add_n(feature_losses, 'perception_loss')

            reconstruction_loss = tf.reduce_mean(tf.reduce_sum(keras.losses.binary_crossentropy(data, reconstruction), axis=(1, 2)))
            kl_loss = -0.5 * (1 + z_log_var - tf.square(z_mean) - tf.exp(z_log_var))
            kl_loss = tf.reduce_mean(tf.reduce_sum(kl_loss, axis=1))
            # Beta VAE
            total_loss = self._total_loss_fn(reconstruction_loss, kl_loss, perception_loss)

        grads = tape.gradient(total_loss, self.trainable_variables, unconnected_gradients=tf.UnconnectedGradients.ZERO)
        self.optimizer.apply_gradients(zip(grads, self.trainable_variables))
        self.total_loss_tracker.update_state(total_loss)
        self.reconstruction_loss_tracker.update_state(reconstruction_loss)
        self.kl_loss_tracker.update_state(kl_loss)
        self.perception_loss_tracker.update_state(perception_loss)

        return {
            "loss": self.total_loss_tracker.result(),
            "reconstruction_loss": self.reconstruction_loss_tracker.result(),
            "kl_loss": self.kl_loss_tracker.result(),
            "perception_loss": self.perception_loss_tracker.result()
        }
