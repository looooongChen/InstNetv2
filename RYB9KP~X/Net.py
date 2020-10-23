import os
import logging
import time
from datetime import datetime
import tensorflow as tf
from fn_loss import build_embedding_loss, build_dist_loss
from fn_loss_disc import build_discriminative_loss
from fn_head import build_embedding_head, build_dist_head, build_dist_concat
from fn_backbone import build_d9, build_Unet
from preprocess import extract_fn

import sys
import numpy as np
import cv2

MAX_IMAGE_SUMMARY = 1

    
class LocalDisNet(object):

    def __init__(self, sess, flags):
        self.sess = sess
        
        # if flags.architecture == 'd7':
        #     self.backbone_fn = build_d7
        # else:
        #     self.backbone_fn = build_d9
        self.backbone_fn = build_d9
        self.backbone_fn2 = build_Unet
        self.flags = flags
        self.dtype = tf.float32

        self.checkpoint_dir = os.path.join(self.flags.model_dir, "checkpoint")
        self.summary_dir = os.path.join(self.flags.model_dir, "summary")

        self.image_w = 512
        self.image_h = 512

    def build_test(self):
        self.input = tf.placeholder(tf.float32,
                                    (None, self.image_h, self.image_w, self.flags.image_channels))
        img_normalized = tf.image.per_image_standardization(self.input)

        features1, features2 = self.backbone_fn(inputs=img_normalized)
        self.embedding1 = build_embedding_head(features1, self.flags.embedding_dim)
        self.dist, f2 = build_dist_head(features2, self.flags.embedding_dim)
        self.embedding2 = build_dist_concat(img_normalized, f2, self.flags.embedding_dim)

       # self.dist_embedding = build_dist_embedding_concat(img_normalized, features2, self.flags.embedding_dim)
        print("embedding branch built.")
        self.saver = tf.train.Saver(max_to_keep=2, name='checkpoint')

    def train(self, batch_size, training_epoches, train_dir, val_dir=None):
        
        ######################
        #### prepare data ####
        ######################

        preprocess_f = lambda sample: extract_fn(sample, 
                                                 image_channels=self.flags.image_channels,
                                                 image_depth=self.flags.image_depth,
                                                 dist_map=self.flags.dist_branch)
        # config training dataset
        train_tf = [os.path.join(train_dir, f) for f in os.listdir(train_dir)]
        train_ds = tf.data.TFRecordDataset(train_tf)
        train_ds = train_ds.map(preprocess_f)
        train_ds = train_ds.shuffle(buffer_size=100)
        train_ds = train_ds.repeat(training_epoches)
        train_ds = train_ds.batch(batch_size)
        train_iterator = train_ds.make_one_shot_iterator()
        train_handle = self.sess.run(train_iterator.string_handle())
        # config validation dataset
        if val_dir is not None:
            val_tf = [os.path.join(val_dir, f) for f in os.listdir(val_dir)]
            val_ds = tf.data.TFRecordDataset(val_tf)
            val_ds = val_ds.map(preprocess_f)
            val_ds = val_ds.batch(batch_size)
            val_iterator = val_ds.make_initializable_iterator()
            val_handle = self.sess.run(val_iterator.string_handle())
        # make iterator        
        handle = tf.placeholder(tf.string, shape=[])
        iterator = tf.data.Iterator.from_string_handle(
            handle, train_ds.output_types, train_ds.output_shapes)
        sample = iterator.get_next()

        ########################################
        #### build the network and training ####
        ########################################

        # prepare aux and summary training data
        self._make_aux()
        img_normalized = tf.image.per_image_standardization(sample['image/image'])
        tf.summary.image('input_image', img_normalized, max_outputs=MAX_IMAGE_SUMMARY)
        tf.summary.image('ground_truth', tf.cast(sample['image/label'] * 10, dtype=tf.uint8), max_outputs=MAX_IMAGE_SUMMARY)
        if self.flags.dist_branch:
            tf.summary.image('distance_map', sample['image/dist_map']*255, max_outputs=MAX_IMAGE_SUMMARY)
        features1, features2= self.backbone_fn(inputs=img_normalized)
        # build embedding branch
        embedding1 = build_embedding_head(features1, self.flags.embedding_dim)
        dist, f2 = build_dist_head(features2, self.flags.embedding_dim )

        embedding_loss,_,_ = build_embedding_loss(embedding1, sample['image/label'], sample['image/neighbor'], include_bg=self.flags.include_bg)
        dist_loss = build_dist_loss(dist, sample['image/dist_map'])
        
        
        embedding2 = build_dist_concat(img_normalized, f2, self.flags.embedding_dim)
        embedding_loss2,loss_inner, loss_inter = build_embedding_loss(embedding2, sample['image/label'], sample['image/neighbor'], include_bg=self.flags.include_bg)
        #dist_embedding = build_dist_embedding_concat(img_normalized, features2, self.flags.embedding_dim)

        ########################
        ####      LOSS      ####
        ########################

        if self.flags.losstype == 'long_loss':
            
            #embedding_loss2 = build_embedding_loss(embedding2, sample['image/label'], sample['image/neighbor'], include_bg=self.flags.include_bg)
            #con_loss = build_embedding_loss(dist_embedding, sample['image/label'], sample['image/neighbor'], include_bg=self.flags.include_bg)
            tf.summary.scalar('loss_embedding', embedding_loss)
            tf.summary.scalar('loss_embedding2', embedding_loss2)
            tf.summary.scalar('loss_embedding_inner', loss_inner)
            tf.summary.scalar('loss_embedding_inter', loss_inter)
            tf.summary.image('emb_dim1-3', embedding1[:, :, :, 0:3], max_outputs=MAX_IMAGE_SUMMARY)
            tf.summary.image('emb2_dim1-3', embedding2[:, :, :, 0:3], max_outputs=MAX_IMAGE_SUMMARY)
            #tf.summary.scalar('loss_con', con_loss)
            #tf.summary.image('emb_con_dim1-3', dist_embedding[:, :, :, 0:3], max_outputs=MAX_IMAGE_SUMMARY)




        ############################
        ####      EU  LOSS      ####
        ############################
        elif self.flags.losstype == 'eucl_loss':
            delta_v = 0.5
            delta_d = 2.
                                
            param_var = 1.
            param_dist = 1.
            param_reg = 0.001

            embedding_loss, l_var, l_dist, l_reg = build_discriminative_loss(  embedding, 
                                                    sample['image/label'], sample['image/neighbor'],                              
                                                    delta_v = delta_v, delta_d = delta_d, # Hyper
                                                    param_var = param_var, param_dist = param_dist, param_reg = param_reg, 
                                                    include_bg=self.flags.include_bg)

            # instance_masks = clustering.get_instance_masks(embedding, bandwidth)
            # for img_id, mask in enumerate(instance_masks):
            #     cv2.imwrite(os.path.join(log_dir, param_string, 'cluster_{}_{}.png'.format(str(step_train).zfill(6), str(img_id)) ), mask)
            tf.summary.scalar('loss_embedding', embedding_loss)

            # @tf.function
            # def pca_trans(embedding):
            #     print("+++++++++++++++")
            #     print(embedding.shape)
            #     pca = PCA(n_components=3)
            #     pca.fit(embedding)
            #     embedding_3d = pca.transform(np.array(embedding[:,:,:,]))
            #     return embedding_3d

            # embedding_3d = pca_trans(embedding)                                                 
            tf.summary.image('embedding_channel_1_3',  embedding[:, :, :, 0:3], max_outputs=MAX_IMAGE_SUMMARY)
            
            print("embedding branch built.")
        ############################
        ####      E N D ...     ####
        ############################



        # build distance regression branch
        if self.flags.dist_branch:
            train_loss = embedding_loss + dist_loss + embedding_loss2
            tf.summary.scalar('loss_dist', dist_loss) 
            tf.summary.image('output_dist',  tf.cast(dist*25.5, dtype=tf.uint8), max_outputs=MAX_IMAGE_SUMMARY)
        else:
            train_loss = embedding_loss + embedding_loss2
        tf.summary.scalar('loss', train_loss)

        # build optimizer
        global_step = tf.Variable(0, trainable=False)
        lr = tf.train.exponential_decay(self.flags.lr, global_step, 5000, 0.9, staircase=True)
        tf.summary.scalar('lr', lr)
        opt = tf.train.AdamOptimizer(learning_rate=lr).minimize(train_loss, global_step=global_step, name="opt")

        # summary and checkpoint
        summary = tf.summary.merge_all()
        train_writer = tf.summary.FileWriter(
            os.path.join(self.summary_dir, 'train'), graph=self.sess.graph)
        if val_dir is not None:
            val_writer = tf.summary.FileWriter(
                os.path.join(self.summary_dir, 'val'), graph=self.sess.graph)
        summary_proto = tf.Summary()

        ########################
        #### start training ####
        ########################

        self.saver = tf.train.Saver(max_to_keep=5, name='checkpoint')
        t_step = self.restore_weights()
        if t_step <= 0:
            self.sess.run(tf.global_variables_initializer())
            logging.info("{}: Init new training".format(datetime.now()))
        try:
            t_time = time.time()
            while True:
                t_step = t_step + 1
                if t_step % self.flags.summary_steps == 0 or t_step == 1:
                    loss, _, c_summary = self.sess.run([train_loss, opt, summary], feed_dict={handle: train_handle})
                    train_writer.add_summary(c_summary, t_step)
                    time_periter = (time.time() - t_time) / self.flags.summary_steps
                    logging.info("{}: Iteration_{} ({:.4f}s/iter)".format(datetime.now(), t_step, time_periter))
                    t_time = time.time()
                else:
                    loss, _ = self.sess.run([train_loss, opt], feed_dict={handle: train_handle})
                    logging.info("Training step {} loss: {}".format(t_step, loss))

                # save checkpoint
                if t_step % self.flags.save_steps == 0:
                    self.saver.save(self.sess, os.path.join(self.checkpoint_dir, 'model'),
                                    global_step=t_step)
                    logging.info("{}: Iteration_{} Saved checkpoint".format(datetime.now(), t_step))

                if val_dir is not None and t_step % self.flags.validation_steps == 0:
                    v_step = 0
                    self.sess.run(val_iterator.initializer)
                    losses = []
                    while True:
                        v_step = v_step + 1
                        try:
                            l = self.sess.run([train_loss], feed_dict={handle: val_handle})
                            losses.append(l)
                            logging.info("Validation step {} loss: {}".format(v_step, l))
                        except Exception as e:
                            val_summary = tf.Summary(value=[
                                tf.Summary.Value(tag="loss_val", simple_value=np.mean(losses))])
                            val_writer.add_summary(val_summary, t_step)
                            break

        except Exception as e:
            logging.info(e)
            logging.info("{}: Done training".format(datetime.now()))

    def restore_model(self, ckp_dir=None):
        self.build_test()
        return self.restore_weights(ckp_dir)

    def restore_weights(self, ckp_dir=None):
        if ckp_dir is None:
            ckp_dir = self.checkpoint_dir
        latest_checkpoint = tf.train.latest_checkpoint(ckp_dir)
        if latest_checkpoint:
            step_num = int(os.path.basename(latest_checkpoint).split("-")[1])
            assert step_num > 0, "Please ensure checkpoint format is model-*.*."
            self.saver.restore(self.sess, latest_checkpoint)
            logging.info("{}: Restore model from step {}. Loaded checkpoint {}"
                         .format(datetime.now(), step_num, latest_checkpoint))
            return step_num
        else:
            return 0

    def _make_aux(self):
        if not os.path.exists(self.summary_dir):
            os.makedirs(self.summary_dir)
        if not os.path.exists(self.checkpoint_dir):
            os.makedirs(self.checkpoint_dir)

        log_file = self.flags.model_dir + "/log.log"
        logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s',
                            filename=log_file,
                            level=logging.DEBUG,
                            filemode='w')
        logging.getLogger().addHandler(logging.StreamHandler())


    def segment_from_seed(self, imgs, seed_thres=0.5, similarity_thres=0.7, resize=True):

        '''
        imgs: list of images (numpy array)
        min_sz: minimal size of object
        resize: resize segments to the same size of imgs
        '''
        import postprocessing as pp
        from skimage.filters import gaussian

        imgs_input = []
        for i in range(len(imgs)): 
            img = np.squeeze(imgs[i])
            if img.shape[0:2] != (self.image_h, self.image_w):
                imgs_input.append(cv2.resize(img, (self.image_h, self.image_w)))
            else:
                imgs_input.append(img)
        imgs_input = np.array(imgs_input)

        if len(imgs_input.shape) == 3:
            imgs_input = np.expand_dims(imgs_input, axis=-1)

        embs, origin_embs, dist = self.sess.run([self.embedding2, self.embedding1, self.dist], feed_dict={self.input: imgs_input})

        segs = []
        for i in range(len(embs)):
            # get seeds
            dist = np.squeeze(gaussian(dist[i], sigma=3))
            seeds = pp.get_seeds(dist, thres=seed_thres)
            # seed to instance mask
            emb = pp.smooth_emb(embs[i], radius=3)
            seg = pp.mask_from_seeds(emb, seeds, similarity_thres=similarity_thres)
            # remove noise
            seg = pp.remove_noise(seg, dist, min_size=10, min_intensity=0.1)
            segs.append(seg)

        if resize:
            for i in range(len(segs)):
                segs[i] = cv2.resize(segs[i], (imgs[i].shape[1], imgs[i].shape[0]), interpolation=cv2.INTER_NEAREST)

        return segs, embs, dist
