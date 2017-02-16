from keras import backend as K
from keras.objectives import categorical_crossentropy


lambda_rpn_regr = 10.0
lambda_rpn_class = 1.0

lambda_cls_regr = 10.0
lambda_cls_class = 1.0


def rpn_loss_regr(num_anchors):
	def rpn_loss_regr_fixed_num(y_true, y_pred):
		x = y_true[:, 4 * num_anchors:, :, :] - y_pred
		x_abs = K.abs(x)
		x_bool = K.lesser_equal(x_abs, 1.0)
		return lambda_rpn_regr * K.sum(
			y_true[:, :4 * num_anchors, :, :] * (x_bool * (0.5 * x * x) + (1 - x_bool) * (x_abs - 0.5))) / 256.
	return rpn_loss_regr_fixed_num


def rpn_loss_cls(num_anchors):
	def rpn_loss_cls_fixed_num(y_true, y_pred):
		return lambda_rpn_class * K.sum(y_true[:, :num_anchors, :, :] * K.binary_crossentropy(y_pred[:, :, :, :], y_true[:, num_anchors:, :, :])) / 256.0
	return rpn_loss_cls_fixed_num


def class_loss_regr(num_rois):
	def class_loss_regr_fixed_num(y_true, y_pred):
		x = y_true[:, :, 4:] - y_pred
		x_abs = K.abs(x)
		x_bool = K.lesser_equal(x_abs, 1.0)
		return lambda_cls_regr * K.sum(y_true[:, :, :4] * (x_bool * (0.5 * x * x) + (1 - x_bool) * (x_abs - 0.5))) / num_rois
	return class_loss_regr_fixed_num


def class_loss_cls(y_true, y_pred):
	return lambda_cls_class * categorical_crossentropy(y_true, y_pred)
