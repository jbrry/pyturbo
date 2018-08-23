from collections import defaultdict
import ad3.factor_graph as fg
from ad3.extensions import PFactorTree, PFactorHeadAutomaton
import numpy as np

from classifier.structured_decoder import StructuredDecoder
from parser.dependency_instance import DependencyInstance
from parser.dependency_parts import DependencyPartArc, \
    DependencyPartLabeledArc, DependencyPartNextSibling, \
    DependencyParts


class DependencyDecoder(StructuredDecoder):

    # this tracks the order in which different types of part are processed
    # (it shall later include grandparents, grandsiblings, etc)
    part_types = [DependencyPartNextSibling]

    def __init__(self):
        StructuredDecoder.__init__(self)

    def decode(self, instance, parts, scores):
        """
        Decode the scores to the dependency parts under the necessary
        contraints, yielding a valid dependency tree.

        :param instance: DependencyInstance
        :param parts: a DependencyParts objects holding all the parts included
            in the scoring functions; usually arcs, siblings and grandparents
        :type parts: DependencyParts
        :param scores: array or tensor with scores for each part, produced by
            the model. It should be a 1d array.
        :return:
        """
        graph = fg.PFactorGraph()
        # graph.set_verbosity(2)
        variables = self.create_tree_factor(instance, parts, scores, graph)

        # create the factors in a predetermined order
        for type_ in self.part_types:
            if type_ == DependencyPartNextSibling:
                self.create_next_sibling_factors(instance, parts, scores,
                                                 graph, variables)

        graph.set_eta_ad3(.05)
        graph.adapt_eta_ad3(True)
        graph.set_max_iterations_ad3(500)
        graph.set_residual_threshold_ad3(1e-3)

        value, posteriors, additional_posteriors, status = \
            graph.solve_lp_map_ad3()
        predicted_output = self.get_predicted_output(parts, posteriors,
                                                     additional_posteriors)

        return predicted_output

    def decode_pruner_naive(self, parts, scores, max_heads):
        """
        Decode the scores generated by the pruner without any constraint on the
        tree structure.

        :param parts: DependencyParts holding arcs
        :param scores: the scores for each part in `parts`
        :param max_heads: maximum allowed head candidates for modifier
        :return:
        """
        candidate_heads = defaultdict(list)
        new_parts = DependencyParts()

        for part, score in zip(parts, scores):
            head = part.head
            modifier = part.modifier
            candidate_heads[modifier].append((head, score))

        for modifier in candidate_heads:
            heads_and_scores = candidate_heads[modifier]
            sorted(heads_and_scores, key=lambda x: x[1], reverse=True)
            heads_and_scores = heads_and_scores[:max_heads]
            for head, score in heads_and_scores:
                new_parts.append(DependencyPartArc(head, modifier))

        return parts

    def get_predicted_output(self, parts, posteriors, additional_posteriors):
        """
        Create a numpy array with the predicted output for each part.

        :param parts: a DependencyParts object
        :param posteriors: list of posterior probabilities of the binary
            variables in the graph
        :param additional_posteriors: list of posterior probabilities of the
            variables introduced by factors
        :return: a numpy array with the same size as parts
        """
        posteriors = np.array(posteriors)
        additional_posteriors = np.array(additional_posteriors)
        predicted_output = np.zeros(len(parts), posteriors.dtype)

        # copy the posteriors and additional to predicted_output in the same
        # order they were created
        offset_arcs, num_arcs = parts.get_offset(DependencyPartArc)
        predicted_output[offset_arcs:offset_arcs + num_arcs] = posteriors

        for type_ in self.part_types:
            offset_type, num_this_type = parts.get_offset(type_)

            from_posteriors = offset_type - num_arcs
            until_posteriors = from_posteriors + num_this_type

            predicted_output[offset_type:offset_type + num_this_type] = \
                additional_posteriors[from_posteriors:until_posteriors]

        return predicted_output

    def create_tree_factor(self, instance, parts, scores, graph):
        """
        Include factors to constrain the graph to a valid dependency tree.

        :type instance: DependencyInstance
        :type parts: DependencyParts
        :param scores: 1d np.array with model scores for each part
        :type graph: fg.PFactorGraph
        :return: a list of arc variables. The i-th variable corresponds to the
            i-th arc in parts.
        """
        # length is the number of tokens in the instance, including root
        length = len(instance)
        offset_arcs, num_arcs = parts.get_offset(DependencyPartArc)

        tree_factor = PFactorTree()
        arc_indices = []
        variables = []
        for r in range(offset_arcs, offset_arcs + num_arcs):
            arc_indices.append((parts[r].head, parts[r].modifier))
            arc_variable = graph.create_binary_variable()
            arc_variable.set_log_potential(scores[r])
            variables.append(arc_variable)

        # owned_by_graph makes the factor persist after calling this function
        # if left as False, the factor is garbage collected
        graph.declare_factor(tree_factor, variables, owned_by_graph=True)
        tree_factor.initialize(length, arc_indices)

        return variables

    def create_next_sibling_factors(self, instance, parts, scores, graph,
                                    variables):
        """
        Include head automata for constraining consecutive siblings in the
        graph.

        :type parts: DependencyParts
        :type instance: DependencyInstance
        :type scores: np.array
        :param graph: the graph
        :param variables: list of binary variables denoting arcs
        """
        if not parts.has_type(DependencyPartNextSibling):
            # there are no consecutive sibling parts
            return

        # needed to map indices in parts to indices in variables
        offset_arcs, _ = parts.get_offset(DependencyPartArc)

        def add_variable_and_arc(local_variables, arcs, h, m):
            """
            Add to `local_variables` the AD3 binary variable representing the
            arc from h to m, and to `arcs` the tuple (h, m).

            If there is not an arc from h to m in `parts`, don't do anything.
            """
            parts_index = parts.find_arc_index(h, m)
            if parts_index < 0:
                return

            # var_index is the index to the binary variable
            var_index = parts_index - offset_arcs
            arc_variable = variables[var_index]
            local_variables.append(arc_variable)
            arcs.append((h, m))

        def set_factor(local_variables, arcs, siblings, scores):
            """
            Create and add a factor head automaton to the graph.
            """
            if len(siblings) == 0:
                return

            # important: first declare the factor in the graph, then initialize
            factor = PFactorHeadAutomaton()
            graph.declare_factor(factor, local_variables, owned_by_graph=True)
            factor.initialize(arcs, siblings, validate=False)
            factor.set_additional_log_potentials(scores)

        n = len(instance)
        offset_siblings, num_siblings = parts.get_offset(
            DependencyPartNextSibling)

        # loop through all parts and organize them according to the head
        left_siblings = create_empty_lists(n)
        right_siblings = create_empty_lists(n)
        left_scores = create_empty_lists(n)
        right_scores = create_empty_lists(n)

        for r in range(offset_siblings, offset_siblings + num_siblings):
            part = parts[r]
            h = part.head
            m = part.modifier
            s = part.sibling

            assert s != h, 'Sibling index cannot be the same as head index'

            if s > h:
                # right sibling
                right_siblings[h].append((h, m, s))
                right_scores[h].append(scores[r])
            else:
                # left sibling
                left_siblings[h].append((h, m, s))
                left_scores[h].append(scores[r])

        # create right and left automata for each head
        for h in range(n):

            # left hand side
            # these are the variables constrained by the factor and their arcs
            local_variables = []

            # these are tuples (h, m)
            arcs = []

            for m in range(h - 1, 0, -1):
                add_variable_and_arc(local_variables, arcs, h, m)

            set_factor(local_variables, arcs, left_siblings[h], left_scores[h])

            # right hand side
            # these are the variables constrained by the factor and their arcs
            local_variables = []
            arcs = []

            for m in range(h + 1, n):
                add_variable_and_arc(local_variables, arcs, h, m)

            set_factor(local_variables, arcs, right_siblings[h],
                       right_scores[h])


def create_empty_lists(n):
    """
    Create a list with n empty lists
    """
    return [[] for _ in range(n)]
