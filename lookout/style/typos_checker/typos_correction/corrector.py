import pickle
import pandas
from tqdm import tqdm
from modelforge import Model
from itertools import chain

from gensim.models import FastText

from typos_correction.utils import suggestions_to_df
from typos_correction.candidates_generation import (CandidatesGenerator, CorrectionsFinder,
                                                    get_candidates_features,
                                                    get_candidates_tokens)
from typos_correction.ranking import CandidatesRanker


class TyposCorrector(Model):
    """
    Model for correcting typos in tokens inside identifiers
    """
    NAME = "typos_correction"
    VENDOR = "source{d}"

    DEFAULT_THREADS_NUMBER = 16

    DEFAULT_RADIUS = 4
    DEFAULT_MAX_DISTANCE = 3
    DEFAULT_NEIGHBORS_NUMBER = 20
    DEFAULT_TAKEN_FOR_DISTANCE = 10

    DEFAULT_TRAIN_ROUNDS = 4000
    DEFAULT_EARLY_STOPPING = 200
    DEFAULT_BOOST_PARAM = {'max_depth': 6,
                           'eta': 0.03,
                           'min_child_weight': 2,
                           'silent': 1,
                           'objective': 'binary:logistic',
                           'nthread': 16,
                           'subsample': 0.5,
                           'colsample_bytree': 0.5,
                           'alpha': 1,
                           'eval_metric': ['auc', 'error']}

    def __init__(self, threads_number: int = DEFAULT_THREADS_NUMBER, nn_file: str=None):
        super().__init__()
        self.nn_file = nn_file
        self.threads_number = threads_number
        self.finder = CorrectionsFinder()
        self.ranker = CandidatesRanker()
        self.set_ranker_params()

    def set_ranker_params(self, train_rounds: int = DEFAULT_TRAIN_ROUNDS,
                            early_stopping: int = DEFAULT_EARLY_STOPPING,
                            boost_param: dict = DEFAULT_BOOST_PARAM) -> None:
        self.ranker.set_boost_params(train_rounds, early_stopping, boost_param)

    def create_model(self, vocabulary_file: str, frequencies_file: str, emb_file: str,
                     neighbors_number: int = DEFAULT_NEIGHBORS_NUMBER,
                     taken_for_distance: int = DEFAULT_TAKEN_FOR_DISTANCE,
                     max_distance: int = DEFAULT_MAX_DISTANCE,
                     radius: int = DEFAULT_RADIUS) -> None:
        self.fasttext = FastText.load_fasttext_format(emb_file)
        self.generator = CandidatesGenerator(self.fasttext, self.nn_file)
        self.finder.construct(vocabulary_file, frequencies_file, self.fasttext, neighbors_number,
                              taken_for_distance, max_distance, radius)

    def _generate_tree(self) -> dict:
        return {"fasttext": pickle.dumps(self.fasttext),
                "finder": self.finder._generate_tree(),
                "ranker": self.ranker._generate_tree()}

    def _load_tree(self, tree: dict) -> None:
        self.fasttext = pickle.loads(tree["fasttext"])
        self.generator = CandidatesGenerator(self.fasttext, self.nn_file)
        finder_dict = tree["finder"]
        finder_dict["fasttext"] = self.fasttext
        self.finder._load_tree(finder_dict)
        self.ranker._load_tree(tree["ranker"])

    def dump(self):
        return ("Candidates and features generator parameters:\n%s"
                "XGBoost classifier is used for ranking candidates" %
                str(self.finder))

    def train(self, typos: pandas.DataFrame, candidates: pandas.DataFrame = None,
              save_candidates_file: str = None) -> None:
        """
        Train corrector on given dataset of typos inside identifiers
        :param typos: pandas.DataFrame containing columns "typo" and "identifier",
               column "token_split" is optional, but used when present
        :param candidates: pandas.DataFrame with precalculated candidates
        :param save_candidates_file: path to file to save candidates to
        :return:
        """
        if candidates is None:
            candidates = self.generator.generate_candidates(typos, self.finder, self.threads_number,
                                                            save_candidates_file)
        self.ranker.fit(typos, get_candidates_tokens(candidates),
                        get_candidates_features(candidates))

    def train_on_file(self, typos_file: str, candidates_file: str = None,
                      save_candidates_file: str = None) -> None:
        typos = pandas.read_csv(typos_file, index_col=0)
        candidates = None
        if candidates_file is not None:
            candidates = pandas.read_pickle(candidates_file)
        self.train(typos, candidates, save_candidates_file)

    def suggest(self, typos: pandas.DataFrame, candidates: pandas.DataFrame = None,
                save_candidates_file: str = None, n_candidates: int = 3,
                return_all: bool = True) -> dict:
        """
        Suggest corrections for given typos
        :param typos: pandas.DataFrame containing column "typo",
               column "token_split" is optional, but used when present
        :param candidates: pandas.DataFrame with precalculated candidates
        :param n_candidates: number of most probable candidates to return
        :param return_all: False to return suggestions only for corrected tokens
        :param save_candidates_file: path to file to save candidates to
        :return:
        """
        if candidates is None:
            candidates = self.generator.generate_candidates(typos, self.finder, self.threads_number,
                                                            save_candidates_file)
        return self.ranker.rank(get_candidates_tokens(candidates),
                                get_candidates_features(candidates), n_candidates, return_all)

    def suggest_file(self, typos_file: str, candidates_file: str = None,
                     save_candidates_file: str = None, n_candidates: int = 3,
                     return_all: bool = True) -> dict:
        typos = pandas.read_csv(typos_file, index_col=0)
        candidates = None
        if candidates_file is not None:
            candidates = pandas.read_pickle(candidates_file)
        return self.suggest(typos, candidates, save_candidates_file, n_candidates, return_all)

    def suggest_by_batches(self, test_df: pandas.DataFrame, n_candidates: int = None,
                           return_all: bool = True, batch_size: int = 2048) -> dict:
        """
        Correct typos from dataset by batches.
        Does not support precalculated candidates.
        """
        all_suggestions = []
        for i in tqdm(range(0, len(test_df), batch_size)):
            suggestions = self.suggest(test_df.loc[test_df.index[i]:
                                                   test_df.index[min(len(test_df) - 1,
                                                                     i + batch_size - 1)], :],
                                       n_candidates=n_candidates, return_all=return_all)
            all_suggestions.append(suggestions.items())

        return dict(list(chain.from_iterable(all_suggestions)))
