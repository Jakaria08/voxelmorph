"""
Example script to train a VoxelMorph model.

For the CVPR and MICCAI papers, we have data arranged in train, validate, and test folders. Inside each folder
are normalized T1 volumes and segmentations in npz (numpy) format. You will have to customize this script slightly
to accommodate your own data. All images should be appropriately cropped and scaled to values between 0 and 1.

If an atlas file is provided with the --atlas flag, then subject-to-atlas training is performed. Otherwise,
registration will be subject-to-subject.
"""

import os
import random
import argparse
import glob
import numpy as np
import keras
import tensorflow as tf
import voxelmorph as vxm


# parse the commandline
parser = argparse.ArgumentParser()

# data organization parameters
parser.add_argument('datadir', help='base data directory')
parser.add_argument('--atlas', help='atlas filename')
parser.add_argument('--model-dir', default='models', help='model output directory (default: models)')

# training parameters
parser.add_argument('--gpu', default='0', help='GPU ID numbers (default: 0)')
parser.add_argument('--batch-size', type=int, default=1, help='batch size (default: 1)')
parser.add_argument('--epochs', type=int, default=1500, help='number of training epochs (default: 1500)')
parser.add_argument('--steps-per-epoch', type=int, default=100, help='frequency of model saves (default: 100)')
parser.add_argument('--load-weights', help='optional weights file to initialize with')
parser.add_argument('--initial-epoch', type=int, default=0, help='initial epoch number (default: 0)')
parser.add_argument('--lr', type=float, default=1e-4, help='learning rate (default: 0.00001)')

# network architecture parameters
parser.add_argument('--enc', type=int, nargs='+', help='list of unet encoder filters (default: 16 32 32 32)')
parser.add_argument('--dec', type=int, nargs='+', help='list of unet decorder filters (default: 32 32 32 32 32 16 16)')
parser.add_argument('--int-steps', type=int, default=7, help='number of integration steps (default: 7)')
parser.add_argument('--int-downsize', type=int, default=2, help='flow downsample factor for integration (default: 2)')
parser.add_argument('--use-probs', action='store_true', help='enable probabilities')
parser.add_argument('--bidir', action='store_true', help='enable bidirectional cost function')

# loss hyperparameters
parser.add_argument('--image-loss', default='mse', help='image reconstruction loss - can be mse or nccc (default: mse)')
parser.add_argument('--lambda', type=float, dest='weight', default=0.01, help='weight of deformation loss (default: 0.01)')
parser.add_argument('--kl-lambda', type=float, default=10, help='prior lambda regularization for KL loss (default: 10)')
parser.add_argument('--legacy-image-sigma', dest='image_sigma', type=float, default=1.0,
                    help='image noise parameter for miccai 2018 network (recommended value is 0.02 when --use-probs is enabled)')
args = parser.parse_args()

batch_size = args.batch_size
bidir = args.bidir

# load and prepare training data
train_vol_names = glob.glob(os.path.join(args.datadir, '*.npz'))
random.shuffle(train_vol_names)  # shuffle volume list
assert len(train_vol_names) > 0, 'Could not find any training data'

if args.atlas:
    # subject-to-atlas generator
    atlas = np.load(args.atlas)['vol'][np.newaxis, ..., np.newaxis]
    generator = vxm.generators.subj2atlas(train_vol_names, atlas, batch_size=args.batch_size, bidir=bidir)
else:
    # subject-to-subject generator
    generator = vxm.generators.subj2subj(train_vol_names, batch_size=args.batch_size, bidir=bidir)

# extract shape from sampled input
inshape = next(generator)[0][0].shape[1:-1]

# prepare model folder
model_dir = args.model_dir
os.makedirs(model_dir, exist_ok=True)

# tensorflow gpu handling
device = '/gpu:' + args.gpu
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
config.allow_soft_placement = True
tf.keras.backend.set_session(tf.Session(config=config))

# ensure valid batch size given gpu count
nb_gpus = len(args.gpu.split(','))
assert np.mod(batch_size, nb_gpus) == 0, 'Batch size (%d) should be a multiple of the number of gpus (%d)' % (batch_size, nb_gpus)

# unet architecture
enc_nf = args.enc if args.enc else [16, 32, 32, 32]
dec_nf = args.dec if args.dec else [32, 32, 32, 32, 32, 16, 16]

# prepare model checkpoint save path
save_filename = os.path.join(model_dir, '{epoch:04d}.h5')

# configure network and save parameters
config = vxm.utils.NetConfig(
    vxm.networks.vxm_net,
    inshape=inshape,
    enc_nf=enc_nf,
    dec_nf=dec_nf,
    bidir=bidir,
    use_probs=args.use_probs,
    int_steps=args.int_steps,
    int_downsize=args.int_downsize
)
config.write(os.path.join(model_dir, 'config.yaml'))

with tf.device(device):

    # build the model from the configuration
    model = config.build_model()

    # load initial weights (if provided)
    if args.load_weights:
        model.load_weights(args.load_weights)

    # prepare image loss
    if args.image_loss == 'ncc':
        image_loss_func = vxm.losses.NCC().loss
    elif args.image_loss == 'mse':
        image_loss_func = vxm.losses.MSE(args.image_sigma).loss
    else:
        raise ValueError('Image loss should be "mse" or "ncc", but found "%s"' % args.image_loss)

    # need two image loss functions if bidirectional
    if bidir:
        losses  = [image_loss_func, image_loss_func]
        weights = [0.5, 0.5]
    else:
        losses  = [image_loss_func]
        weights = [1]

    # prepare deformation loss
    if args.use_probs:
        flow_shape = model.outputs[-1].shape[1:-1]
        deformation_loss_func = vxm.losses.KL(args.kl_lambda, flow_shape).loss
    else:
        deformation_loss_func = vxm.losses.Grad('l2').loss

    losses  += [deformation_loss_func]
    weights += [args.weight]

    # multi-gpu support
    if nb_gpus > 1:
        save_callback = vxm.networks.ModelCheckpointParallel(save_filename)
        model = keras.utils.multi_gpu_model(model, gpus=nb_gpus)
    else:
        save_callback = keras.callbacks.ModelCheckpoint(save_filename, save_weights_only=True)

    model.compile(optimizer=keras.optimizers.Adam(lr=args.lr), loss=losses, loss_weights=weights)

    # save starting weights
    model.save(save_filename.format(epoch=args.initial_epoch))

    model.fit_generator(generator,
        initial_epoch=args.initial_epoch,
        epochs=args.epochs,
        steps_per_epoch=args.steps_per_epoch,
        callbacks=[save_callback],
        verbose=1
    )

    # save final model weights
    model.save(save_filename.format(epoch=args.epochs))
