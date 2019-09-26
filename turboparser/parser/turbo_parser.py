# -*- coding: utf-8 -*-

from ..classifier import utils
from ..classifier.instance import InstanceData
from .token_dictionary import TokenDictionary
from .constants import Target, target2string
from .dependency_reader import read_instances
from .dependency_writer import DependencyWriter
from .dependency_decoder import DependencyDecoder, chu_liu_edmonds_one_root, \
    make_score_matrix, chu_liu_edmonds
from .dependency_parts import DependencyParts
from .dependency_neural_model import DependencyNeuralModel
from .dependency_scorer import DependencyNeuralScorer, get_gold_tensors
from .dependency_instance_numeric import DependencyInstanceNumeric
from .constants import SPECIAL_SYMBOLS, EOS, EMPTY

from collections import defaultdict
import pickle
import numpy as np
import time


logger = utils.get_logger()


class ModelType(object):
    """Dummy class to store the types of parts used by a parser"""
    def __init__(self, type_string):
        """
        :param type_string: a string encoding multiple types of parts:
            af: arc factored (always used)
            cs: consecutive siblings
            gp: grandparents
            as: arbitrary siblings
            hb: head bigrams
            gs: grandsiblings
            ts: trisiblings

            More than one type must be concatenated by +, e.g., af+cs+gp
        """
        codes = type_string.lower().split('+')
        self.consecutive_siblings = 'cs' in codes
        self.grandparents = 'gp' in codes
        self.grandsiblings = 'gs' in codes
        self.arbitrary_siblings = 'as' in codes
        self.head_bigrams = 'hb' in codes
        self.trisiblings = 'ts' in codes

class TurboParser(object):
    '''Dependency parser.'''
    def __init__(self, options):
        self.options = options
        self.token_dictionary = TokenDictionary()
        self.writer = DependencyWriter()
        self.decoder = DependencyDecoder()
        self.model = None
        self._set_options()
        self.neural_scorer = DependencyNeuralScorer()

        if self.options.train:
            pretrain_words, pretrain_embeddings = self._load_embeddings()
            self.token_dictionary.initialize(
                self.options.training_path, self.options.case_sensitive,
                pretrain_words, char_cutoff=options.char_cutoff,
                morph_cutoff=options.morph_tag_cutoff,
                form_cutoff=options.form_cutoff,
                lemma_cutoff=options.lemma_cutoff)

            model = DependencyNeuralModel(
                self.model_type,
                self.token_dictionary, pretrain_embeddings,
                char_hidden_size=self.options.char_hidden_size,
                transform_size=self.options.transform_size,
                trainable_word_embedding_size=self.options.embedding_size,
                char_embedding_size=self.options.char_embedding_size,
                tag_embedding_size=self.options.tag_embedding_size,
                distance_embedding_size=self.options.
                distance_embedding_size,
                rnn_size=self.options.rnn_size,
                arc_mlp_size=self.options.arc_mlp_size,
                label_mlp_size=self.options.label_mlp_size,
                ho_mlp_size=self.options.ho_mlp_size,
                rnn_layers=self.options.rnn_layers,
                mlp_layers=self.options.mlp_layers,
                dropout=self.options.dropout,
                word_dropout=options.word_dropout,
                tag_mlp_size=options.tag_mlp_size,
                predict_upos=options.upos,
                predict_xpos=options.xpos,
                predict_morph=options.morph)

            self.neural_scorer.initialize(
                model, self.options.normalization, self.options.learning_rate,
                options.decay, options.beta1, options.beta2)

            if self.options.verbose:
                logger.info('Model summary:')
                logger.info(str(model))

    def _create_random_embeddings(self):
        """
        Create random embeddings for the vocabulary of the token dict.
        """
        num_words = self.token_dictionary.get_num_embeddings()
        dim = self.options.embedding_size
        embeddings = np.random.normal(0, 0.1, [num_words, dim])
        return embeddings

    def _load_embeddings(self):
        """
        Load word embeddings and dictionary. If they are not used, return both
        as None.

        :return: word dictionary, numpy embedings
        """
        logger.info('Loading embeddings')
        if self.options.embeddings is not None:
            words, embeddings = utils.read_embeddings(self.options.embeddings,
                                                      SPECIAL_SYMBOLS)
        else:
            words = None
            embeddings = None
        logger.info('Loaded')

        return words, embeddings

    def _set_options(self):
        """
        Set some parameters of the parser determined from its `options`
        attribute.
        """
        self.model_type = ModelType(self.options.model_type)
        self.has_pruner = bool(self.options.pruner_path)

        if self.options.model_type != 'af':
            if self.options.normalization == 'local':
                msg = 'Local normalization not implemented for ' \
                      'higher order models'
                logger.error(msg)
                exit(1)

            if not self.has_pruner:
                msg = 'Running higher-order model without pruner! ' \
                      'Parser may be very slow!'
                logger.warning(msg)

        if self.has_pruner:
            self.pruner = load_pruner(self.options.pruner_path)
            if self.options.pruner_batch_size > 0:
                self.pruner.options.batch_size = self.options.pruner_batch_size
        else:
            self.pruner = None

        self.additional_targets = []
        if self.options.morph:
            self.additional_targets.append(Target.MORPH)
        if self.options.upos:
            self.additional_targets.append(Target.UPOS)
        if self.options.xpos:
            self.additional_targets.append(Target.XPOS)

    def save(self, model_path=None):
        """Save the full configuration and model."""
        if not model_path:
            model_path = self.options.model_path
        with open(model_path, 'wb') as f:
            pickle.dump(self.options, f)
            self.token_dictionary.save(f)
            self.neural_scorer.model.save(f)

    @classmethod
    def load(cls, options):
        """Load the full configuration and model."""
        with open(options.model_path, 'rb') as f:
            loaded_options = pickle.load(f)

            options.model_type = loaded_options.model_type
            options.unlabeled = loaded_options.unlabeled
            options.morph = loaded_options.morph
            options.xpos = loaded_options.xpos
            options.upos = loaded_options.upos

            # backwards compatibility
            # TODO: change old saved models to include normalization model
            options.normalization = loaded_options.normalization \
                if hasattr(loaded_options, 'normalization') else 'local'

            # threshold for the basic pruner, if used
            options.pruner_posterior_threshold = \
                loaded_options.pruner_posterior_threshold

            # maximum candidate heads per word in the basic pruner, if used
            options.pruner_max_heads = loaded_options.pruner_max_heads

            parser = TurboParser(options)
            parser.token_dictionary.load(f)

            model = DependencyNeuralModel.load(f, parser.token_dictionary)

        parser.neural_scorer.set_model(model)
        parser.neural_scorer.normalization = options.normalization

        # most of the time, we load a model to run its predictions
        parser.neural_scorer.eval_mode()

        return parser

    def _reset_best_validation_metric(self):
        """
        Set the best validation UAS score to 0
        """
        self.best_validation_uas = 0.
        self.best_validation_las = 0.
        self._should_save = False

    def get_gold_labels(self, instance):
        """
        Return a list of dictionary mapping the name of each target to a numpy
        vector with the gold values.

        :param instance: DependencyInstanceNumeric
        :return: dict
        """
        gold_dict = {}

        # [1:] to skip root symbol
        if self.options.upos:
            gold_dict[Target.UPOS] = instance.get_all_upos()[1:]
        if self.options.xpos:
            gold_dict[Target.XPOS] = instance.get_all_xpos()[1:]
        if self.options.morph:
            gold_dict[Target.MORPH] = instance.get_all_morph_singletons()[1:]

        gold_dict[Target.HEADS] = instance.get_all_heads()[1:]
        gold_dict[Target.RELATIONS] = instance.get_all_relations()[1:]

        return gold_dict

    def run_pruner(self, instances):
        """
        Prune out some arcs with the pruner model.

        :param instance: a list of DependencyInstance objects, not formatted
        :return: a list of boolean 2d arrays masking arcs, one for each
            instance. It has shape (n, n) where n is the instance length
            including root. Position (h, m) has True if the arc is valid, False
            otherwise. During training, gold arcs always are True.
        """
        pruner = self.pruner
        instance_data = pruner.preprocess_instances(instances, report=False)
        instance_data.prepare_batches(pruner.options.batch_size, sort=False)
        masks = []
        entropies = []

        for batch in instance_data.batches:
            batch_masks, batch_entropies = self.prune_batch(batch)
            masks.extend(batch_masks)
            entropies.extend(batch_entropies)

        entropies = np.array(entropies)
        logger.info('Pruner mean entropy: %f' % entropies.mean())

        return masks

    def prune_batch(self, instance_data):
        """
        Prune out some possible arcs in the given instances.

        This function runs the encapsulated pruner.

        :param instance_data: a InstanceData object
        :return: a tuple (masks, entropies)
            masks: a list of  boolean 2d array masking arcs. It has shape (n, n)
            where n is the instance length including root. Position (h, m) has
            True if the arc is valid, False otherwise.

            entropies: a list of the tree entropies found by the matrix tree
            theorem
        """
        pruner = self.pruner
        scores = pruner.neural_scorer.compute_scores(instance_data,
                                                     argmax_tags=False)
        masks = []
        entropies = []

        for i in range(len(scores)):
            inst_scores = scores[i]
            inst_parts = instance_data.parts[i]

            if pruner.options.normalization == 'local':
                # if the pruner was trained with local normalization, its output
                # for arcs is a (n - 1, n) matrix. Convert it to a list of part
                # scores. First, transpose (modifier, head) to (head, modifier)
                head_scores = inst_scores[Target.HEADS].T
                label_scores = inst_scores[Target.RELATIONS].transpose(1, 0, 2)

                # the existing mask only masks out self attachments and root
                # attachments. It is useful to turn a matrix into a vector.
                # the first column in the mask can be removed
                mask = inst_parts.arc_mask[:, 1:]
                inst_scores[Target.HEADS] = head_scores[mask]
                inst_scores[Target.RELATIONS] = label_scores[mask].reshape(-1)

            new_mask, entropy = self.decoder.decode_matrix_tree(
                inst_parts, inst_scores, self.options.pruner_max_heads,
                self.options.pruner_posterior_threshold)

            if self.options.train:
                # if training, put back any gold arc pruned out
                instance = instance_data.instances[i]
                for m in range(1, len(instance)):
                    h = instance.heads[m]
                    if not new_mask[h, m]:
                        new_mask[h, m] = True
                        self.pruner_mistakes += 1

            masks.append(new_mask)
            entropies.append(entropy)

        return masks, entropies

    def _report_make_parts(self, data):
        """
        Log some statistics about the calls to make parts in a dataset.

        :type data: InstanceData
        """
        num_arcs = 0
        num_tokens = 0
        num_possible_arcs = 0
        num_higher_order = defaultdict(int)

        for instance, inst_parts in zip(data.instances, data.parts):
            inst_len = len(instance)
            num_inst_tokens = inst_len - 1  # exclude root
            num_tokens += num_inst_tokens
            num_possible_arcs += num_inst_tokens ** 2

            mask = inst_parts.arc_mask
            num_arcs += mask.sum()

            for part_type in inst_parts.part_lists:
                num_parts = len(inst_parts.part_lists[part_type])
                num_higher_order[part_type] += num_parts

        msg = '%f heads per token after pruning' % (num_arcs / num_tokens)
        logger.info(msg)

        msg = '%d arcs after pruning, out of %d possible (%f)' % \
              (num_arcs, num_possible_arcs, num_arcs / num_possible_arcs)
        logger.info(msg)

        for part_type in num_higher_order:
            num = num_higher_order[part_type]
            name = target2string[part_type]
            msg = '%d %s parts' % (num, name)
            logger.info(msg)

        if self.options.train:
            ratio = (num_tokens - self.pruner_mistakes) / num_tokens
            msg = 'Pruner recall (gold arcs retained after pruning): %f' % ratio
            logger.info(msg)

    def decode_predictions(self, predicted_parts, parts, head_score_matrix=None,
                           label_matrix=None):
        """
        Decode the predicted heads and labels over the output of the AD3 decoder
        or just score matrices.

        This function takes care of the cases when the variable assignments by
        the decoder does not produce a valid tree running the Chu-Liu-Edmonds
        algorithm.

        :param predicted_parts: indicator array of predicted dependency parts
            (with values between 0 and 1)
        :param parts: the dependency parts
        :type parts: DependencyParts
        :return: a tuple (pred_heads, pred_labels)
            The first is an array such that position heads[m] contains the head
            for token m; it starts from the first actual word, not the root.
            If the model is not trained for predicting labels, the second item
            is None.
        """
        if head_score_matrix is None:
            length = len(parts.arc_mask)
            arc_scores = predicted_parts[:parts.num_arcs]
            score_matrix = make_score_matrix(length, parts.arc_mask, arc_scores)
        else:
            # TODO: provide the matrix already (n x n)
            zeros = np.zeros_like(head_score_matrix[0]).reshape([1, -1])
            score_matrix = np.concatenate([zeros, head_score_matrix], 0)

        if self.options.single_root:
            pred_heads = chu_liu_edmonds_one_root(score_matrix)
        else:
            pred_heads = chu_liu_edmonds(score_matrix)

        pred_heads = pred_heads[1:]
        if parts.labeled:
            if label_matrix is not None:
                pred_labels = []
                for m, h in enumerate(pred_heads):
                    pred_labels.append(label_matrix[m, h])

                pred_labels = np.array(pred_labels)
            else:
                pred_labels = parts.get_labels(pred_heads)
        else:
            pred_labels = None

        return pred_heads, pred_labels

    def compute_validation_metrics(self, valid_data, valid_pred):
        """
        Compute and store internally validation metrics. Also call the neural
        scorer to update learning rate.

        At least the UAS is computed. Depending on the options, also LAS and
        tagging accuracy.

        :param valid_data: InstanceData
        :type valid_data: InstanceData
        :param valid_pred: list with predicted outputs (decoded) for each item
            in the data. Each item is a dictionary mapping target names to the
            prediction vectors.
        """
        accumulated_uas = 0.
        accumulated_las = 0.
        accumulated_tag_hits = {target: 0.
                                for target in self.additional_targets}
        total_tokens = 0

        for i in range(len(valid_data)):
            instance = valid_data.instances[i]
            gold_output = valid_data.gold_labels[i]
            inst_pred = valid_pred[i]

            real_length = len(instance) - 1
            gold_heads = gold_output[Target.HEADS]
            pred_heads = inst_pred[Target.HEADS][:real_length]

            # scale UAS by sentence length; it is normalized later
            head_hits = gold_heads == pred_heads
            accumulated_uas += np.sum(head_hits)
            total_tokens += real_length

            if not self.options.unlabeled:
                pred_labels = inst_pred[Target.RELATIONS][:real_length]
                deprel_gold = gold_output[Target.RELATIONS]
                label_hits = deprel_gold == pred_labels
                label_head_hits = np.logical_and(head_hits, label_hits)
                accumulated_las += np.sum(label_head_hits)

            for target in self.additional_targets:
                target_gold = gold_output[target]
                target_pred = inst_pred[target][:real_length]
                hits = target_gold == target_pred
                accumulated_tag_hits[target] += np.sum(hits)

        self.validation_uas = accumulated_uas / total_tokens
        self.validation_las = accumulated_las / total_tokens
        self.validation_accuracies = {}
        for target in self.additional_targets:
            self.validation_accuracies[target] = accumulated_tag_hits[
                                       target] / total_tokens

        # always update UAS; use it as a criterion for saving if no LAS
        if self.validation_uas >= self.best_validation_uas:
            self.best_validation_uas = self.validation_uas
            improved_uas = True
        else:
            improved_uas = False

        if self.options.unlabeled:
            self._should_save = improved_uas
        else:
            if self.validation_las >= self.best_validation_las:
                self.best_validation_las = self.validation_las
                self._should_save = True
            else:
                self._should_save = False

    def enforce_well_formed_graph(self, instance, arcs):
        if self.options.projective:
            raise NotImplementedError
        else:
            return self.enforce_connected_graph(instance, arcs)

    def enforce_connected_graph(self, instance, arcs):
        '''Make sure the graph formed by the unlabeled arc parts is connected,
        otherwise there is no feasible solution.
        If necessary, root nodes are added and passed back through the last
        argument.'''
        inserted_arcs = []
        # Create a list of children for each node.
        children = [[] for i in range(len(instance))]
        for r in range(len(arcs)):
            assert type(arcs[r]) == Arc
            children[arcs[r].head].append(arcs[r].modifier)

        # Check if the root is connected to every node.
        visited = [False] * len(instance)
        nodes_to_explore = [0]
        while nodes_to_explore:
            h = nodes_to_explore.pop(0)
            visited[h] = True
            for m in children[h]:
                if visited[m]:
                    continue
                nodes_to_explore.append(m)
            # If there are no more nodes to explore, check if all nodes
            # were visited and, if not, add a new edge from the node to
            # the first node that was not visited yet.
            if not nodes_to_explore:
                for m in range(1, len(instance)):
                    if not visited[m]:
                        logging.info('Inserted root node 0 -> %d.' % m)
                        inserted_arcs.append((0, m))
                        nodes_to_explore.append(m)
                        break

        return inserted_arcs

    def run(self):
        self.reassigned_roots = 0
        tic = time.time()

        instances = read_instances(self.options.test_path)
        logger.info('Number of instances: %d' % len(instances))
        data = self.preprocess_instances(instances)
        data.prepare_batches(self.options.batch_size, sort=False)
        predictions = []

        for batch in data.batches:
            batch_predictions = self.run_batch(batch)
            predictions.extend(batch_predictions)

        self.write_predictions(instances, data.parts, predictions)
        toc = time.time()
        logger.info('Time: %f' % (toc - tic))

    def write_predictions(self, instances, parts, predictions):
        """
        Write predictions to a file.

        :param instances: the instances in the original format (i.e., not the
            "formatted" one, but retaining the original contents)
        :param parts: list with the parts per instance
        :param predictions: list with predictions per instance
        """
        self.writer.open(self.options.output_path)
        for instance, inst_parts, inst_prediction in zip(instances,
                                                         parts, predictions):
            self.label_instance(instance, inst_parts, inst_prediction)
            self.writer.write(instance)

        self.writer.close()

    def read_train_instances(self):
        '''Create batch of training and validation instances.'''
        import time
        tic = time.time()
        logger.info('Creating instances...')

        train_instances = read_instances(self.options.training_path)
        valid_instances = read_instances(self.options.valid_path)
        logger.info('Number of train instances: %d' % len(train_instances))
        logger.info('Number of validation instances: %d'
                     % len(valid_instances))
        toc = time.time()
        logger.info('Time: %f' % (toc - tic))
        return train_instances, valid_instances

    def preprocess_instances(self, instances, report=True):
        """
        Create parts for all instances in the batch.

        :param instances: list of non-formatted Instance objects
        :param report: log the number of created parts and pruner errors. It
            should be False in a pruner model.
        :return: an InstanceData object.
            It contains formatted instances.
            In neural models, features is a list of None.
        """
        all_parts = []
        all_gold_labels = []
        formatted_instances = []
        self.pruner_mistakes = 0
        num_relations = self.token_dictionary.get_num_deprels()
        labeled = not self.options.unlabeled

        if self.has_pruner:
            prune_masks = self.run_pruner(instances)
        else:
            prune_masks = None

        for i, instance in enumerate(instances):
            mask = None if prune_masks is None else prune_masks[i]
            numeric_instance = DependencyInstanceNumeric(
                instance, self.token_dictionary, self.options.case_sensitive)
            parts = DependencyParts(numeric_instance, self.model_type, mask,
                                    labeled, num_relations)
            gold_labels = self.get_gold_labels(numeric_instance)

            formatted_instances.append(numeric_instance)
            all_parts.append(parts)
            all_gold_labels.append(gold_labels)

        data = InstanceData(formatted_instances, all_parts, all_gold_labels)
        if report:
            self._report_make_parts(data)
        return data

    def reset_performance_metrics(self):
        """
        Reset some variables used to keep track of training performance.
        """
        self.num_train_instances = 0
        self.time_scores = 0
        self.time_decoding = 0
        self.time_gradient = 0
        self.train_losses = defaultdict(float)
        self.accumulated_hits = {}
        for target in self.additional_targets:
            self.accumulated_hits[target] = 0

        self.accumulated_uas = 0.
        self.accumulated_las = 0.
        self.total_tokens = 0
        self.validation_uas = 0.
        self.validation_las = 0.
        self.reassigned_roots = 0

    def train(self):
        '''Train with a general online algorithm.'''
        train_instances, valid_instances = self.read_train_instances()
        logger.info('Preprocessing training data')
        train_data = self.preprocess_instances(train_instances)
        logger.info('\nPreprocessing validation data')
        valid_data = self.preprocess_instances(valid_instances)
        train_data.prepare_batches(self.options.batch_size, sort=True)
        valid_data.prepare_batches(self.options.batch_size, sort=True)
        logger.info('Training data spread across %d batches'
                     % len(train_data.batches))
        logger.info('Validation data spread across %d batches\n'
                     % len(valid_data.batches))

        self._reset_best_validation_metric()
        self.reset_performance_metrics()
        using_amsgrad = False
        num_bad_evals = 0

        for global_step in range(1, self.options.max_steps + 1):
            batch = train_data.get_next_batch()
            self.train_batch(batch)

            if global_step % self.options.log_interval == 0:
                msg = 'Step %d' % global_step
                logger.info(msg)
                self.train_report(self.num_train_instances)
                self.reset_performance_metrics()

            if global_step % self.options.eval_interval == 0:
                self.run_on_validation(valid_data)
                if self._should_save:
                    self.save()
                    num_bad_evals = 0
                else:
                    num_bad_evals += 1
                    self.neural_scorer.decrease_learning_rate()

                if num_bad_evals == self.options.patience:
                    if not using_amsgrad:
                        logger.info('Switching to AMSGrad')
                        using_amsgrad = True
                        self.neural_scorer.switch_to_amsgrad(
                            self.options.learning_rate, self.options.beta1,
                            self.options.beta2)
                        num_bad_evals = 0
                    else:
                        break

        msg = 'Best validation UAS: %f' % self.best_validation_uas
        if not self.options.unlabeled:
            msg += '\tBest validation LAS: %f' % self.best_validation_las
        logger.info(msg)

    def run_on_validation(self, valid_data):
        """
        Run the model on validation data
        """
        valid_start = time.time()
        self.neural_scorer.eval_mode()

        predictions = []
        for batch in valid_data.batches:
            batch_predictions = self.run_batch(batch)
            predictions.extend(batch_predictions)

        self.compute_validation_metrics(valid_data, predictions)

        valid_end = time.time()
        time_validation = valid_end - valid_start

        logger.info('Time to run on validation: %.2f' % time_validation)

        msgs = ['Validation accuracies:\tUAS: %.4f' % self.validation_uas]
        if not self.options.unlabeled:
            msgs.append('LAS: %.4f' % self.validation_las)
        for target in self.additional_targets:
            target_name = target2string[target]
            acc = self.validation_accuracies[target]
            msgs.append('%s: %.4f' % (target_name, acc))
        logger.info('\t'.join(msgs))

        if self._should_save:
            logger.info('Saved model')

        logger.info('\n')

    def train_report(self, num_instances):
        """
        Log a short report of the training loss.
        """
        msgs = ['Train losses:'] + make_loss_msgs(self.train_losses,
                                                  num_instances)
        logger.info('\t'.join(msgs))

        time_msg = 'Time to score: %.2f\tDecode: %.2f\tGradient step: %.2f'
        time_msg %= (self.time_scores, self.time_decoding, self.time_gradient)
        logger.info(time_msg)

    def run_batch(self, instance_data):
        """
        Predict the output for the given instances.

        :type instance_data: InstanceData
        :return: a list of arrays with the predicted outputs if return_loss is
            False. If it's True, a tuple with predictions and losses.
            Each prediction is a dictionary mapping a target name to the
            prediction vector.
        """
        self.neural_scorer.eval_mode()
        scores = self.neural_scorer.compute_scores(instance_data,
                                                   argmax_tags=True)

        predictions = []
        for i in range(len(instance_data)):
            instance = instance_data.instances[i]
            parts = instance_data.parts[i]
            inst_scores = scores[i]

            if self.options.normalization == 'global':
                predicted_parts = self.decoder.decode(
                    instance, parts, inst_scores)
                head_scores = None
                label_scores = None
            else:
                predicted_parts = None
                head_scores = inst_scores[Target.HEADS]
                label_scores = inst_scores[Target.RELATIONS]

            pred_heads, pred_labels = self.decode_predictions(
                predicted_parts, parts, head_scores, label_scores)

            inst_prediction = {Target.HEADS: pred_heads,
                               Target.RELATIONS: pred_labels}

            for target in self.additional_targets:
                # argmax is computed in the scorer
                inst_prediction[target] = inst_scores[target]

            predictions.append(inst_prediction)

        return predictions

    def train_batch(self, instance_data):
        '''
        Run one batch of a learning algorithm. If it is an online one, just
        run through each instance.

        :param instance_data: InstanceData object containing the instances of
            the batch
        '''
        self.neural_scorer.train_mode()

        start_time = time.time()

        # scores is a list of dictionaries [target] -> score array
        scores = self.neural_scorer.compute_scores(instance_data)
        end_time = time.time()
        self.time_scores += end_time - start_time

        all_predicted_parts = []
        if self.options.normalization == 'global':
            # need to decode sentences in order to compute global loss
            for i in range(len(instance_data)):
                instance = instance_data.instances[i]
                parts = instance_data.parts[i]
                inst_scores = scores[i]

                predicted_parts = self.decode_train(
                    instance, parts, inst_scores)
                all_predicted_parts.append(predicted_parts)

        # run the gradient step for the whole batch
        start_time = time.time()
        losses = self.neural_scorer.compute_loss(instance_data,
                                                 all_predicted_parts)
        self.neural_scorer.make_gradient_step(losses)
        batch_size = len(instance_data)
        self.num_train_instances += batch_size
        for target in losses:
            # store non-normalized losses
            self.train_losses[target] += batch_size * losses[target].item()

        end_time = time.time()
        self.time_gradient += end_time - start_time

    def decode_train(self, instance, parts, scores):
        """
        Decode the scores for parsing at training time.

        Return the predicted output (for each part)

        :param instance: a DependencyInstanceNumeric
        :param parts: DependencyParts
        :type parts: DependencyParts
        :param scores: a dictionary mapping target names to scores produced by
            the network. The scores must be 1-d arrays (matrixes should be
            converted)
        :return: prediction array
        """
        # Do the decoding.
        start_decoding = time.time()
        predicted_output = self.decoder.decode(instance, parts, scores)

        end_decoding = time.time()
        self.time_decoding += end_decoding - start_decoding

        return predicted_output

    def label_instance(self, instance, parts, output):
        """
        :type instance: DependencyInstance
        :type parts: DependencyParts
        :param output: dictionary mapping target names to predictions
        :return:
        """
        heads = output[Target.HEADS]
        relations = output[Target.RELATIONS]

        for m, h in enumerate(heads, 1):
            instance.heads[m] = h

            if parts.labeled:
                relation = relations[m - 1]
                relation_name = self.token_dictionary.deprel_alphabet.\
                    get_label_name(relation)
                instance.relations[m] = relation_name

            if self.options.upos:
                # -1 because there's no tag for the root
                tag = output[Target.UPOS][m - 1]
                tag_name = self.token_dictionary. \
                    upos_alphabet.get_label_name(tag)
                instance.upos[m] = tag_name
            if self.options.xpos:
                tag = output[Target.XPOS][m - 1]
                tag_name = self.token_dictionary. \
                    xpos_alphabet.get_label_name(tag)
                instance.xpos[m] = tag_name
            if self.options.morph:
                tag = output[Target.MORPH][m - 1]
                tag_name = self.token_dictionary. \
                    morph_singleton_alphabet.get_label_name(tag)
                instance.morph_singletons[m] = tag_name


def load_pruner(model_path):
    """
    Load and return a pruner model.

    This function takes care of keeping the main parser and the pruner
    configurations separate.
    """
    logger.info('Loading pruner from %s' % model_path)
    with open(model_path, 'rb') as f:
        pruner_options = pickle.load(f)

    pruner_options.train = False
    pruner_options.model_path = model_path
    pruner = TurboParser.load(pruner_options)

    return pruner


def make_loss_msgs(losses, dataset_size):
    """
    Return a list of strings in the shape

    NAME: LOSS_VALUE

    :param losses: dictionary mapping targets to loss values
    :param dataset_size: value used to normalize (divide) each loss value
    :return: list of strings
    """
    msgs = []
    for target in losses:
        target_name = target2string[target]
        normalized_loss = losses[target] / dataset_size
        msg = '%s: %.4f' % (target_name, normalized_loss)
        msgs.append(msg)
    return msgs
