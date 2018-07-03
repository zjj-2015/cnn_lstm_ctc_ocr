# CNN-LSTM-CTC-OCR
# Copyright (C) 2017 Jerod Weinman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import time
import tensorflow as tf
from tensorflow.contrib import learn

import mjsynth
import model

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string('model','../data/model',
                          """Directory for model checkpoints""")

tf.app.flags.DEFINE_integer('batch_size',2**9,
                            """Eval batch size""")

tf.app.flags.DEFINE_string('device','/gpu:0',
                           """Device for graph placement""")

tf.app.flags.DEFINE_string('test_path','../data/',
                           """Base directory for test/validation data""")
tf.app.flags.DEFINE_string('filename_pattern','test/words-*',
                           """File pattern for input data""")
tf.app.flags.DEFINE_integer('num_input_threads',4,
                          """Number of readers for input data""")

tf.logging.set_verbosity(tf.logging.WARN)

# Non-configurable parameters
mode = learn.ModeKeys.INFER # 'Configure' training mode for dropout layers


def _get_input():
    """Set up and return image, label, width and text tensors"""

    image,width,label,length,text,filename=mjsynth.threaded_input_pipeline(
        FLAGS.test_path,
        str.split(FLAGS.filename_pattern,','),
        batch_size=FLAGS.batch_size,
        num_threads=FLAGS.num_input_threads,
        num_epochs=1 )
    
    return image,width,label,length

def _get_session_config():
    """Setup session config to soften device placement"""
    config=tf.ConfigProto(
        allow_soft_placement=True, 
        log_device_placement=False)

    return config

def _get_testing(rnn_logits,sequence_length,label,label_length):
    """Create ops for testing (all scalars): 
       label_error:  Normalized edit distance on beam search max
       sequence_error: Normalized sequence error rate
    """
    with tf.name_scope("eval"):
        predictions,_ = tf.nn.ctc_beam_search_decoder(rnn_logits, 
                                                   sequence_length,
                                                   beam_width=128,
                                                   top_paths=1,
                                                   merge_repeated=True)
        hypothesis = tf.cast(predictions[0], tf.int32) # for edit_distance

        # Per-sequence statistic
        num_label_errors = tf.edit_distance(hypothesis, label, normalize=False)
        # Per-batch summary counts
        batch_num_label_errors = tf.reduce_sum( num_label_errors )
        batch_num_sequence_errors = tf.count_nonzero(num_label_errors,axis=0)
        batch_num_labels = tf.reduce_sum( label_length )
        batch_size = tf.shape(label_length)[0]

        # Wide unsigned integer type casts
        batch_num_label_errors = tf.cast( batch_num_label_errors, tf.int64 )
        batch_num_sequence_errors = tf.cast( batch_num_sequence_errors, tf.int64)
        batch_num_labels = tf.cast( batch_num_labels, tf.int64 )
        batch_size = tf.cast( batch_size, tf.int64 )

        # Variables to tally across batches (all initially zero)
        total_num_label_errors = tf.Variable(0, trainable=False,
                                             name='total_num_label_errors',
                                             dtype=tf.int64)
        total_num_sequence_errors = tf.Variable(0, trainable=False,
                                                name='total_num_sequence_errors',
                                                dtype=tf.int64)
        total_num_labels =  tf.Variable(0, trainable=False,
                                        name='total_num_labels',
                                        dtype=tf.int64)

        total_num_sequences =  tf.Variable(0, trainable=False,
                                           name='total_num_sequences',
                                           dtype=tf.int64)

        # Create the "+=" update ops and group together as one
        update_label_errors    = tf.assign_add( total_num_label_errors,
                                                batch_num_label_errors )
        update_num_labels      = tf.assign_add( total_num_labels,
                                                batch_num_labels )
        update_sequence_errors = tf.assign_add( total_num_sequence_errors,
                                                batch_num_sequence_errors )
        update_num_sequences   = tf.assign_add( total_num_sequences,
                                                batch_size )

        update_metrics = tf.group( update_label_errors,
                                   update_num_labels,
                                   update_sequence_errors,
                                   update_num_sequences )

        metrics = [total_num_label_errors, 
                   total_num_labels,
                   total_num_sequence_errors,
                   total_num_sequences]

        # Tensors to make final calculations
        label_error = tf.truediv( total_num_label_errors, 
                                  total_num_labels,
                                  name='label_error')
        sequence_error = tf.truediv( total_num_sequence_errors,
                                     total_num_sequences,
                                     name='sequence_error')
                   
    return label_error, sequence_error, update_metrics, metrics

def _get_checkpoint():
    """Get the checkpoint path from the given model output directory"""
    ckpt = tf.train.get_checkpoint_state(FLAGS.model)

    if ckpt and ckpt.model_checkpoint_path:
        ckpt_path=ckpt.model_checkpoint_path
    else:
        raise RuntimeError('No checkpoint file found')

    return ckpt_path

def _get_init_trained():
    """Return init function to restore trained model from a given checkpoint"""

    # Gross hack so the saver doesn't try to load the tally variables. Improvable?
    saver_reader = tf.train.Saver(
        [var for var in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES) 
         if "eval" not in var.name]
    )
        #tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
    #)
    
    init_fn = lambda sess,ckpt_path: saver_reader.restore(sess, ckpt_path)
    return init_fn

def main(argv=None):

    with tf.Graph().as_default():

        with tf.device(FLAGS.device):

            image,width,label,length = _get_input()
            features,sequence_length = model.convnet_layers( image, width, mode)
            logits = model.rnn_layers( features, sequence_length,
                                       mjsynth.num_classes() )
            label_error,sequence_error,update_metrics,metrics = _get_testing(
                logits,sequence_length,label,length)

        session_config = _get_session_config()
        restore_model = _get_init_trained()

        init_op = tf.group( tf.global_variables_initializer(),
                            tf.local_variables_initializer()) 

        with tf.Session(config=session_config) as sess:
            
            sess.run(init_op)

            coord = tf.train.Coordinator() # Launch reader threads
            threads = tf.train.start_queue_runners(sess=sess,coord=coord)
            
            restore_model(sess, _get_checkpoint()) # Get latest checkpoint

            try:            
                while not coord.should_stop():
                    sess.run(update_metrics)
            except tf.errors.OutOfRangeError:
                # Indicates that the single epoch is complete.
                0 # NOP
            finally:
                coord.request_stop()

            final_vals = sess.run([metrics,label_error,sequence_error])
            print final_vals

        coord.join(threads)

if __name__ == '__main__':
    tf.app.run()
