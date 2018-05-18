import pandas as pd
import networkx as nx
import logging, yaml
from .transformer import Transformer

from neo4j.v1 import GraphDatabase

class NeoTransformer(Transformer):
    """
    TODO: use bolt

    We expect a Translator canonical style http://bit.ly/tr-kg-standard
    E.g. predicates are names with underscores, not IDs.

    Does not load from config file if uri and username are provided.

    TODO: also support mapping from Monarch neo4j
    """

    def __init__(self, t=None, uri=None, username=None, password=None):
        super(NeoTransformer, self).__init__(t)

        if uri is username is None:
            with open("config.yml", 'r') as ymlfile:
                cfg = yaml.load(ymlfile)
                uri = "bolt://{}:{}".format(cfg['neo4j']['host'], cfg['neo4j']['port'])
                username = cfg['neo4j']['username']
                password = cfg['neo4j']['password']

        self.driver = GraphDatabase.driver(uri, auth=(username, password))


    def load(self):
        """
        Read a neo4j database and create a nx graph
        """

        with self.driver.session() as session:
            self.load_nodes(session.read_transaction(self.get_nodes))
            self.load_edges(session.read_transaction(self.get_edges))

    def load_nodes(self, node_records):
        """
        Load nodes from neo4j records
        """

        for node in node_records:
            self.load_node(node)

    def load_edges(self, edge_records):
        """
        Load edges from neo4j records
        """

        for edge in edge_records:
            self.load_edge(edge)

    def load_node(self, node_record):
        """
        Load node from a neo4j record
        """

        node=node_record[0]

        attributes = {
            'labels' : list(node.labels),
            'properties' : node.properties
        }

        self.graph.add_node(node.id, attr_dict=attributes)

    def load_edge(self, edge_record):
        """
        Load an edge from a neo4j record
        """

        edge_subject = edge_record[0]
        edge_predicate = edge_record[1]
        edge_object = edge_record[2]

        attributes = {
                'id' : edge_predicate.id,
                'type' : edge_predicate.type,
                'properties' : edge_predicate.properties
        }

        self.graph.add_edge(
            edge_subject.id,
            edge_object.id,
            attr_dict=attributes
        )

    def get_nodes(self, tx):
        """
        Get all nodes from neo4j database
        """

        labels = None
        if 'subject_category' in self.filter:
            labels = self.filter['subject_category']
        if 'object_category' in self.filter:
            labels += ':' + self.filter['object_category']

        properties = {}
        for key in self.filter:
            if key not in ['subject_category', 'object_category', 'edge_label']:
                properties[key] = self.filter[key]

        query = None
        if labels:
            query = "MATCH (n:{labels} {{ {properties} }}) RETURN n".format(labels=labels, properties=self.parse_properties(properties))
        else:
            query = "MATCH (n {{ {properties} }}) RETURN n".format(properties=self.parse_properties(properties))

        logging.debug(query)
        return tx.run(query)

    def get_edges(self, tx):
        """
        Get all edges from neo4j database
        """

        params = {}
        params['subject_category'] = self.filter['subject_category'] if 'subject_category' in self.filter else None
        params['object_category'] = self.filter['object_category'] if 'object_category' in self.filter else None
        params['edge_label'] = self.filter['edge_label'] if 'edge_label' in self.filter else None

        properties = {}
        for key in self.filter:
            if key not in ['subject_category', 'object_category', 'edge_label']:
                properties[key] = self.filter[key]

        query = None
        if params['subject_category'] is not None and params['object_category'] is not None and params['edge_label'] is not None:
            query = "MATCH (s:{subject_category})-[p:{edge_label} {{ {edge_properties} }}]->(o:{object_category}) RETURN s,p,o".format(**params, edge_properties=self.parse_properties(properties))
        elif params['subject_category'] is None and params['object_category'] is not None and params['edge_label'] is not None:
            query = "MATCH (s)-[p:{edge_label} {{ {edge_properties} }}]->(o:{object_category}) RETURN s,p,o".format(**params, edge_properties=self.parse_properties(properties))
        elif params['subject_category'] is None and params['object_category'] is None and params['edge_label'] is not None:
            query = "MATCH (s)-[p:{edge_label} {{ {edge_properties} }}]->(o) RETURN s,p,o".format(**params, edge_properties=self.parse_properties(properties))
        else:
            query = "MATCH (s)-[p {{ {edge_properties} }}]->(o) RETURN s,p,o".format(**params, edge_properties=self.parse_properties(properties))

        logging.debug(query)
        return tx.run(query)

    def save_node(self, tx, obj):
        """
        Load a node into neo4j
        """

        if 'id' not in obj:
            raise KeyError("node does not have 'id' property")
        if 'name' not in obj:
            logging.warning("node does not have 'name' property")

        if 'category' not in obj:
            logging.warning("node does not have 'category' property. Using 'named_thing' as default")
            label = 'named_thing'
        else:
            label = obj['category']
            del obj['category']

        query = "CREATE (n:{label} {{ {properties} }})".format(label = label, properties = self.parse_properties(obj))
        tx.run(query)

    def save_node_unwind(self, nodes_by_category, property_names):
        """
        Save all nodes into neo4j using the UNWIND cypher clause
        """

        for category in nodes_by_category.keys():
            self.populate_missing_properties(nodes_by_category[category], property_names)
            query = self.generate_unwind_node_query(category, property_names)
            with self.driver.session() as session:
                session.run(query, nodes=nodes_by_category[category])

    def save_edge_unwind(self, edges_by_relationship_type, property_names):
        """
        Save all edges into neo4j using the UNWIND cypher clause
        """

        for predicate in edges_by_relationship_type:
            self.populate_missing_properties(edges_by_relationship_type[predicate], property_names)
            query = self.generate_unwind_edge_query(predicate, property_names)
            edges = edges_by_relationship_type[predicate]
            with self.driver.session() as session:
                session.run(query, relationship=predicate, edges=edges)

    def generate_unwind_node_query(self, label, property_names):
        """
        Generate UNWIND cypher clause for a given label and property names (optional)
        """

        properties_dict = {}
        ignore_list = []
        for property in property_names:
            if property not in ignore_list:
                properties_dict[property] = "node.{}".format(property)

        query = "UNWIND $nodes as node CREATE (p:{label} {node_properties})".format(label=label, node_properties=str(properties_dict).replace("'", ""))
        logging.debug(query)
        return query


    def generate_unwind_edge_query(self, relationship, property_names):
        """
        Generate UNWIND cypher clause for a given relationship
        """

        properties_dict = {}
        ignore_list = ['subject', 'predicate', 'object']
        for property in property_names:
            if property not in ignore_list:
                properties_dict[property] = "edge.{}".format(property)

        query = "UNWIND $edges as edge MATCH (a {{ id: edge.subject }}) MATCH (b {{ id: edge.object }}) MERGE (a)-[r:{relationship} {relationship_properties} ]->(b)".format(relationship=relationship, relationship_properties=str(properties_dict).replace("'",""))
        logging.debug(query)
        return query

    def save_edge(self, tx, obj):
        """
        Load an edge into neo4j
        """

        queryString = "MATCH (s {{ id: '{subject_id}' }}) MATCH (o {{ id: '{object_id}' }}) MERGE (s)-[r:{relationship} {{ {relationship_properties} }}]->(o)"

        query_params = {
            'subject_id': obj['subject'], 'object_id': obj['object'],
            'relationship': obj['predicate'], 'relationship_properties': self.parse_properties(obj)
        }

        query = queryString.format(**query_params)
        logging.debug(query)
        tx.run(query)

    def save_from_csv(self, nodes_filename, edges_filename):
        """
        Load from a CSV to neo4j
        """
        nodes_df = pd.read_csv(nodes_filename)
        edges_df = pd.read_csv(edges_filename)

        with self.driver.session() as session:
            for index, row in nodes_df.iterrows():
                session.write_transaction(self.save_node, row.to_dict())
            for index, row in edges_df.iterrows():
                session.write_transaction(self.save_edge, row.to_dict())
        self.neo4j_report()

    def save_with_unwind(self):
        """
        Load from a nx graph to neo4j using the UNWIND cypher clause
        """

        nodes_by_category = {}
        node_properties = []
        for n in self.graph.nodes():
            node = self.graph.node[n]
            if 'id' not in node:
                continue
            if 'category' not in node:
                node['category'] = 'named_thing'
            if node['category'] not in nodes_by_category:
                nodes_by_category[node['category']] = [node]
            else:
                nodes_by_category[node['category']].append(node)
            node_properties += [x for x in node if x not in node_properties]

        edges_by_relationship_type = {}
        edge_properties = []
        for n, nbrs in self.graph.adjacency_iter():
            for nbr, eattr in nbrs.items():
                for entry, adjitem in eattr.items():
                    print(adjitem)
                    if adjitem['predicate'] not in edges_by_relationship_type:
                        edges_by_relationship_type[adjitem['predicate']] = [adjitem]
                    else:
                        edges_by_relationship_type[adjitem['predicate']].append(adjitem)
                    edge_properties += [x for x in adjitem.keys() if x not in edge_properties]

        with self.driver.session() as session:
            session.write_transaction(self.create_constraints, nodes_by_category.keys())

        self.save_node_unwind(nodes_by_category, node_properties)
        self.save_edge_unwind(edges_by_relationship_type, edge_properties)

    def save(self):
        """
        Load from a nx graph to neo4j
        """

        labels = {'named_thing'}
        for n in self.graph.nodes():
            node = self.graph.node[n]
            if 'category' in node:
                labels.add(node['category'])


        with self.driver.session() as session:
            session.write_transaction(self.create_constraints, labels)
            for n in self.graph.nodes():
                node_attributes = self.graph.node[n]
                session.write_transaction(self.save_node, node_attributes)
            for n, nbrs in self.graph.adjacency_iter():
                for nbr, eattr in nbrs.items():
                    for entry, adjitem in eattr.items():
                        session.write_transaction(self.save_edge, adjitem)
        self.neo4j_report()

    def report(self):
        print("Total number of nodes: {}".format(len(self.graph.nodes())))
        print("Total number of edges: {}".format(len(self.graph.edges())))

    def neo4j_report(self):
        """
        Give a summary on the number of nodes and edges in neo4j database
        """
        with self.driver.session() as session:
            for r in session.run("MATCH (n) RETURN COUNT(*)"):
                logging.info("Number of Nodes: {}".format(r.values()[0]))
            for r in session.run("MATCH (s)-->(o) RETURN COUNT(*)"):
                logging.info("Number of Edges: {}".format(r.values()[0]))

    def create_constraints(self, tx, labels):
        """
        Create a unique constraint on node 'id' for all labels
        """

        query = "CREATE CONSTRAINT ON (n:{}) ASSERT n.id IS UNIQUE"
        for label in labels:
            tx.run(query.format(label))

    @staticmethod
    def parse_properties(properties, delim = '|'):
        propertyList = []
        for key in properties:
            if key in ['subject', 'predicate', 'object']:
                continue

            values = properties[key]
            if type(values) == type(""):
                pair = "{}: \"{}\"".format(key, str(values))
            else:
                pair = "{}: {}".format(key, str(values))
            propertyList.append(pair)
        return ','.join(propertyList)

    @staticmethod
    def populate_missing_properties(objs, properties):
        for obj in objs:
            missing_properties = set(properties) - set(obj.keys())
            for property in missing_properties:
                obj[property] = ''

class MonarchNeoTransformer(NeoTransformer):
    """
    TODO: do we need a subclass, or just make parent configurable?

    In contrast to a generic import/export, the Monarch neo4j graph
    uses reification (same as Richard's semmeddb implementation in neo4j).
    This transform should de-reify.

    Also:

     - rdf:label to name
     - neo4j label to category
    """
