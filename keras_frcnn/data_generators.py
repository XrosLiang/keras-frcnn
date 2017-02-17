import numpy as np
import cv2
import random
import copy
import data_augment
import threading
import itertools
#import numba



def get_img_output_length(width, height):
	def get_output_length(input_length):
		# zero_pad
		input_length += 6
		# apply 4 strided convolutions
		filter_sizes = [7, 3, 1, 1]
		stride = 2
		for filter_size in filter_sizes:
			input_length = (input_length - filter_size + stride) // stride
		return input_length

	return get_output_length(width), get_output_length(height)

#@numba.jit
def union(au, bu):
	x = min(au[0], bu[0])
	y = min(au[1], bu[1])
	w = max(au[2], bu[2]) - x
	h = max(au[3], bu[3]) - y
	return x, y, w, h

#@numba.jit
def intersection(ai, bi):
	x = max(ai[0], bi[0])
	y = max(ai[1], bi[1])
	w = min(ai[2], bi[2]) - x
	h = min(ai[3], bi[3]) - y
	if w < 0 or h < 0:
		return 0, 0, 0, 0
	return x, y, w, h

#@numba.jit
def iou(a, b):
	# a and b should be (x1,y1,x2,y2)
	assert a[0] < a[2]
	assert a[1] < a[3]
	assert b[0] < b[2]
	assert b[1] < b[3]

	i = intersection(a, b)
	u = union(a, b)

	area_i = i[2] * i[3]
	area_u = u[2] * u[3]
	return float(area_i) / float(area_u)


def get_new_img_size(width, height, img_min_side=600):
	if width <= height:
		f = float(img_min_side) / width
		resized_height = int(f * height)
		resized_width = img_min_side
	else:
		f = float(img_min_side) / height
		resized_width = int(f * width)
		resized_height = img_min_side

	return resized_width, resized_height




class SampleSelector:
	def __init__(self, class_count):
		# ignore classes that have zero samples
		self.classes = [b for b in class_count.keys() if class_count[b] != 0]
		self.class_cycle = itertools.cycle(self.classes)

	def skip_sample_for_balanced_class(self, img_data):

		curr_class = self.class_cycle.next()
		class_in_img = False

		for bbox in img_data['bboxes']:

			cls_name = bbox['class']

			if cls_name == curr_class:
				class_in_img = True
				break

		if class_in_img:
			return False
		else:
			return True


def calcY(C, class_mapping, img_data, width, height, resized_width, resized_height):
	downscale = float(C.rpn_stride)
	anchor_sizes = C.anchor_box_scales
	anchor_ratios = C.anchor_box_ratios
	num_anchors = len(anchor_sizes) * len(anchor_ratios)	

	# calculate the output map size based on the network architecture
	(output_width, output_height) = get_img_output_length(resized_width, resized_height)

	n_anchratios = len(anchor_ratios)
	
	# initialise empty output objectives
	y_rpn_overlap = np.zeros((output_height, output_width, num_anchors)).astype(int)
	y_is_box_valid = np.zeros((output_height, output_width, num_anchors)).astype(int)
	y_rpn_regr = np.zeros((output_height, output_width, num_anchors * 4))

	num_bboxes = len(img_data['bboxes'])

	num_anchors_for_bbox = np.zeros(num_bboxes).astype(int)
	best_anchor_for_bbox = -1*np.ones((num_bboxes, 4)).astype(int)
	best_iou_for_bbox = np.zeros(num_bboxes)
	best_x_for_bbox = np.zeros((num_bboxes, 4))
	best_dx_for_bbox = np.zeros((num_bboxes, 4))

	pos_samples = []
	cls_samples = []
	cls_regr_samples = []
	neg_samples = []

	# get the GT box coordinates, and resize to account for image resizing
	gta = np.zeros((num_bboxes, 4))
	for bbox_num, bbox in enumerate(img_data['bboxes']):
		# get the GT box coordinates, and resize to account for image resizing
		gta[bbox_num, 0] = bbox['x1'] * (resized_width / float(width))
		gta[bbox_num, 1] = bbox['x2'] * (resized_width / float(width))
		gta[bbox_num, 2] = bbox['y1'] * (resized_height / float(height))
		gta[bbox_num, 3] = bbox['y2'] * (resized_height / float(height))

	
	for anchor_size_idx in xrange(len(anchor_sizes)):
		for anchor_ratio_idx in xrange(n_anchratios):
			anchor_x = anchor_sizes[anchor_size_idx] * anchor_ratios[anchor_ratio_idx][0]
			anchor_y = anchor_sizes[anchor_size_idx] * anchor_ratios[anchor_ratio_idx][1]	
			
			for ix in xrange(output_width):					
				# x-coordinates of the current anchor box	
				x1_anc = downscale * (ix + 0.5) - anchor_x / 2
				x2_anc = downscale * (ix + 0.5) + anchor_x / 2	
				
				# ignore boxes that go across image boundaries					
				if x1_anc < 0 or x2_anc > resized_width:
					continue
					
				for jy in xrange(output_height):

					# y-coordinates of the current anchor box
					y1_anc = downscale * (jy + 0.5) - anchor_y / 2
					y2_anc = downscale * (jy + 0.5) + anchor_y / 2

					# ignore boxes that go across image boundaries
					if y1_anc < 0 or y2_anc > resized_height:
						continue

					# bbox_type indicates whether an anchor should be a target 
					bbox_type = 'neg'

					# this is the best IOU for the (x,y) coord and the current anchor
					# note that this is different from the best IOU for a GT bbox
					best_iou_for_loc = 0.0

					for bbox_num in xrange(num_bboxes):
						
						# get IOU of the current GT box and the current anchor box
						curr_iou = iou([gta[bbox_num, 0], gta[bbox_num, 2], gta[bbox_num, 1], gta[bbox_num, 3]], [x1_anc, y1_anc, x2_anc, y2_anc])
						# calculate the regression targets if they will be needed
						if curr_iou > best_iou_for_bbox[bbox_num] or curr_iou > 0.5:
							tx = (gta[bbox_num, 0] - x1_anc) / (x2_anc - x1_anc)
							ty = (gta[bbox_num, 2] - y1_anc) / (y2_anc - y1_anc)
							#calculate log of tw and th later
							tw = 1.0*(gta[bbox_num, 1] - gta[bbox_num, 0]) / (x2_anc - x1_anc) 
							th = 1.0*(gta[bbox_num, 3] - gta[bbox_num, 2]) / (y2_anc - y1_anc) 
						
						if img_data['bboxes'][bbox_num]['class'] != 'bg':

							# all GT boxes should be mapped to an anchor box, so we keep track of which anchor box was best
							if curr_iou > best_iou_for_bbox[bbox_num]:
								best_anchor_for_bbox[bbox_num] = [jy, ix, anchor_ratio_idx, anchor_size_idx]
								best_iou_for_bbox[bbox_num] = curr_iou
								best_x_for_bbox[bbox_num] = [x1_anc, x2_anc, y1_anc, y2_anc]
								best_dx_for_bbox[bbox_num] = [tx, ty, tw, th]

							# we set the anchor to positive if the IOU is >0.7 (it does not matter if there was another better box, it just indicates overlap)
							if curr_iou > 0.7:
								bbox_type = 'pos'
								num_anchors_for_bbox[bbox_num] += 1
								# we update the regression layer target if this IOU is the best for the current (x,y) and anchor position
								if curr_iou > best_iou_for_loc:
									best_iou_for_loc = curr_iou
									best_regr = (tx, ty, tw, th)

							# if the IOU is >0.3 and <0.7, it is ambiguous and no included in the objective
							if 0.3 < curr_iou < 0.7:
								# gray zone between neg and pos
								if bbox_type != 'pos':
									bbox_type = 'neutral'


						# we also make a list of anchor boxes that can be used as positive or negative targets for the classification network
						if curr_iou < 0.1:
							# negative sample, we dont store this since it's probably very 'easy'
							pass
						elif curr_iou < 0.5:
							# sample which partially overlaps a GT box. We store this since we expect it to be a rather difficult background sample
							neg_samples.append((int(x1_anc / downscale), int(y1_anc / downscale),
												int((x2_anc - x1_anc) / downscale),
												int((y2_anc - y1_anc) / downscale)))
						else:
							# A positive sample, there is sufficient overlap
							pos_samples.append((int(x1_anc / downscale), int(y1_anc / downscale), int((x2_anc - x1_anc) / downscale), int((y2_anc - y1_anc) / downscale)))
							cls_samples.append(img_data['bboxes'][bbox_num]['class'])
							cls_regr_samples.append([tx,ty,tw,th])

					# turn on or off outputs depending on IOUs
					if bbox_type == 'neg':
						y_is_box_valid[jy, ix, anchor_ratio_idx + n_anchratios * anchor_size_idx] = 1
						y_rpn_overlap[jy, ix, anchor_ratio_idx + n_anchratios * anchor_size_idx] = 0
					elif bbox_type == 'neutral':
						y_is_box_valid[jy, ix, anchor_ratio_idx + n_anchratios * anchor_size_idx] = 0
						y_rpn_overlap[jy, ix, anchor_ratio_idx + n_anchratios * anchor_size_idx] = 0
					else:
						y_is_box_valid[jy, ix, anchor_ratio_idx + n_anchratios * anchor_size_idx] = 1
						y_rpn_overlap[jy, ix, anchor_ratio_idx + n_anchratios * anchor_size_idx] = 1
						start = 4 * anchor_ratio_idx + 4 * n_anchratios * anchor_size_idx
						y_rpn_regr[jy, ix, start:start+2 ] = best_regr[0:2]
						y_rpn_regr[jy, ix, start+2:start+4 ] = np.log(best_regr[2:])


	#check that there is at least one labeled region in the image
	if len(pos_samples) == 0:
		return None, None, None, None, None

	# we ensure that every bbox has at least one positive RPN region
	for idx in xrange(num_anchors_for_bbox.shape[0]):
		if num_anchors_for_bbox[idx] == 0:
			# no box with an IOU greater than zero ...
			if best_anchor_for_bbox[idx, 0] == -1:
				continue
			y_is_box_valid[
				best_anchor_for_bbox[idx,0], best_anchor_for_bbox[idx,1], best_anchor_for_bbox[idx,2] + n_anchratios *
				best_anchor_for_bbox[idx,3]] = 1
			y_rpn_overlap[
				best_anchor_for_bbox[idx,0], best_anchor_for_bbox[idx,1], best_anchor_for_bbox[idx,2] + n_anchratios *
				best_anchor_for_bbox[idx,3]] = 1
			start = 4 * best_anchor_for_bbox[idx,2] + 4 * n_anchratios * best_anchor_for_bbox[idx,3] + 0	
			y_rpn_regr[
				best_anchor_for_bbox[idx,0], best_anchor_for_bbox[idx,1], start:start+2] = best_dx_for_bbox[idx, 0:2]
			y_rpn_regr[
				best_anchor_for_bbox[idx,0], best_anchor_for_bbox[idx,1], start+2:start+4] = np.log(best_dx_for_bbox[idx, 2:4])


	y_rpn_overlap = np.transpose(y_rpn_overlap, (2, 0, 1))
	y_rpn_overlap = np.expand_dims(y_rpn_overlap, axis=0)

	y_is_box_valid = np.transpose(y_is_box_valid, (2, 0, 1))
	y_is_box_valid = np.expand_dims(y_is_box_valid, axis=0)

	y_rpn_regr = np.transpose(y_rpn_regr, (2, 0, 1))
	y_rpn_regr = np.expand_dims(y_rpn_regr, axis=0)

	pos_locs = np.where(np.logical_and(y_rpn_overlap[0, :, :, :] == 1, y_is_box_valid[0, :, :, :] == 1))
	neg_locs = np.where(np.logical_and(y_rpn_overlap[0, :, :, :] == 0, y_is_box_valid[0, :, :, :] == 1))
		
	pos_samples = np.array(pos_samples)
	neg_samples = np.array(neg_samples)
	cls_samples = np.array(cls_samples)
	cls_regr_samples = np.array(cls_regr_samples)

	# randomly sample some positive and negative ROIs from the list
	target_pos_samples = C.num_rois / 2

	if pos_samples.shape[0] > target_pos_samples:
		val_locs = random.sample(range(pos_samples.shape[0]), target_pos_samples)
		valid_pos_samples = pos_samples[val_locs, :]
		valid_cls_samples = cls_samples[val_locs]
		valid_regr_samples = cls_regr_samples[val_locs,:]
	else:
		valid_pos_samples = pos_samples
		valid_cls_samples = cls_samples
		valid_regr_samples = cls_regr_samples

	val_locs = random.sample(range(neg_samples.shape[0]), C.num_rois - valid_cls_samples.shape[0])
	valid_neg_samples = neg_samples[val_locs, :]

	valid_regr_samples[:, 2:] = np.log(valid_regr_samples[:, 2:])

	x_rois = np.expand_dims(np.concatenate([valid_pos_samples, valid_neg_samples]), axis=0)
	
	y_class_num = np.zeros((x_rois.shape[1], len(class_mapping)))
	# regr has 8 values: 4 for on/off, 4 for w,y,w,h
	y_class_regr = np.zeros((x_rois.shape[1], 2*4))

	for i in xrange(x_rois.shape[1]):
		if i < valid_cls_samples.shape[0]:
			class_num = class_mapping[valid_cls_samples[i]]
			y_class_num[i, class_num] = 1
		else:
			y_class_num[i, -1] = 1
		# NB: we only y_class_regr set to positive here if the sample is not from the bg class
		if y_class_num[i, -1] != 1:
			y_class_regr[i, :4] = 1 # set value to 1 if the sample is positive
			y_class_regr[i,4:] = valid_regr_samples[i,:]
			

	y_class_num = np.expand_dims(y_class_num, axis=0)
	y_class_regr = np.expand_dims(y_class_regr, axis=0)

	num_pos = len(pos_locs[0])

	# one issue is that the RPN has many more negative than positive regions, so we turn off some of the negative regions

	if len(pos_locs[0]) > 128:
		val_locs = random.sample(range(len(pos_locs[0])), len(pos_locs[0]) - 128)
		y_is_box_valid[0, pos_locs[0][val_locs], pos_locs[1][val_locs], pos_locs[2][val_locs]] = 0
		num_pos = 128

	if len(neg_locs[0]) + num_pos > 256:
		val_locs = random.sample(range(len(neg_locs[0])), len(neg_locs[0]) + num_pos - 256)
		y_is_box_valid[0, neg_locs[0][val_locs], neg_locs[1][val_locs], neg_locs[2][val_locs]] = 0

	y_rpn_cls = np.concatenate([y_is_box_valid, y_rpn_overlap], axis=1)
	y_rpn_regr = np.concatenate([np.repeat(y_rpn_overlap, 4, axis=1), y_rpn_regr], axis=1)

	return x_rois, y_rpn_cls, y_rpn_regr, y_class_num, y_class_regr


class threadsafe_iter:
	"""Takes an iterator/generator and makes it thread-safe by
	serializing call to the `next` method of given iterator/generator.
	"""
	def __init__(self, it):
		self.it = it
		self.lock = threading.Lock()

	def __iter__(self):
		return self

	def next(self):
		with self.lock:
			return self.it.next()		

	
def threadsafe_generator(f):
	"""A decorator that takes a generator function and makes it thread-safe.
	"""
	def g(*a, **kw):
		return threadsafe_iter(f(*a, **kw))
	return g

#@threadsafe_generator
def get_anchor_gt(all_img_data, class_mapping, class_count, C, mode='train'):
	downscale = float(C.rpn_stride)

	anchor_sizes = C.anchor_box_scales
	anchor_ratios = C.anchor_box_ratios

	num_anchors = len(anchor_sizes) * len(anchor_ratios)

	sample_selector = SampleSelector(class_count)

	while True:
		if mode=='train':
			random.shuffle(all_img_data)

		for img_data in all_img_data:

			if C.balanced_classes and sample_selector.skip_sample_for_balanced_class(img_data):
				continue

			# read in image, and optionally add augmentation

			if mode=='train':
				img_data_aug, x_img = data_augment.augment(img_data, C, augment=True)
			else:
				img_data_aug, x_img = data_augment.augment(img_data, C, augment=False)

			(width, height) = (img_data_aug['width'], img_data_aug['height'])
			(rows, cols, _) = x_img.shape

			assert cols == width
			assert rows == height

			# get image dimensions for resizing
			(resized_width, resized_height) = get_new_img_size(width, height, C.im_size)

			# resize the image so that smalles side is length = 600px
			x_img = cv2.resize(x_img, (resized_width, resized_height), interpolation=cv2.INTER_CUBIC)

			# calculate the output map size based on the network architecture
			(output_width, output_height) = get_img_output_length(resized_width, resized_height)

			x_rois, y_rpn_cls, y_rpn_regr, y_class_num, y_class_regr = calcY(C, class_mapping, img_data_aug, width, height, resized_width, resized_height)
			if x_rois is None:
				continue

			x_img = np.transpose(x_img, (2, 0, 1))
			x_img = np.expand_dims(x_img, axis=0).astype('float32')

			# Zero-center by mean pixel
			x_img[:, 0, :, :] -= 103.939
			x_img[:, 1, :, :] -= 116.779
			x_img[:, 2, :, :] -= 123.68

			yield [x_img, x_rois], [y_rpn_cls, y_rpn_regr, y_class_num, y_class_regr]



