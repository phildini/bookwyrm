""" test searching for books """
import datetime
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from bookwyrm import book_search, models
from bookwyrm.connectors.abstract_connector import AbstractMinimalConnector


class BookSearch(TestCase):
    """look for some books"""

    @classmethod
    def setUpTestData(self):  # pylint: disable=bad-classmethod-argument
        """we need basic test data and mocks"""
        self.work = models.Work.objects.create(title="Example Work")

        self.first_edition = models.Edition.objects.create(
            title="Example Edition",
            parent_work=self.work,
            isbn_10="0000000000",
            physical_format="Paperback",
            published_date=datetime.datetime(2019, 4, 9, 0, 0, tzinfo=timezone.utc),
        )
        self.second_edition = models.Edition.objects.create(
            title="Another Edition",
            parent_work=self.work,
            isbn_10="1111111111",
            openlibrary_key="hello",
            pages=150,
        )
        self.third_edition = models.Edition.objects.create(
            title="Another Edition with annoying ISBN",
            parent_work=self.work,
            isbn_10="022222222X",
        )

    def test_search(self):
        """search for a book in the db"""
        # title/author
        results = book_search.search("Example")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], self.first_edition)

        # isbn
        results = book_search.search("0000000000")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], self.first_edition)

        # identifier
        results = book_search.search("hello")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], self.second_edition)

    def test_isbn_search(self):
        """test isbn search"""
        results = book_search.isbn_search("0000000000")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], self.first_edition)

    def test_search_identifiers(self):
        """search by unique identifiers"""
        results = book_search.search_identifiers("hello")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], self.second_edition)

    def test_search_identifiers_isbn_search(self):
        """search by unique ID with slightly wonky ISBN"""
        results = book_search.search_identifiers("22222222x")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], self.third_edition)

    def test_search_identifiers_return_first(self):
        """search by unique identifiers"""
        result = book_search.search_identifiers("hello", return_first=True)
        self.assertEqual(result, self.second_edition)

    def test_search_title_author(self):
        """search by unique identifiers"""
        results = book_search.search_title_author("annoying", min_confidence=0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], self.third_edition)

    def test_search_title_author_return_first(self):
        """sorts by edition rank"""
        result = book_search.search_title_author(
            "Another", min_confidence=0, return_first=True
        )
        self.assertEqual(result, self.second_edition)  # highest edition rank

    def test_search_title_author_one_edition_per_work(self):
        """at most one edition per work"""
        results = book_search.search_title_author("Edition", 0)
        self.assertEqual(results, [self.first_edition])  # highest edition rank

    def test_format_search_result(self):
        """format a search result"""
        result = book_search.format_search_result(self.first_edition)
        self.assertEqual(result["title"], "Example Edition")
        self.assertEqual(result["key"], self.first_edition.remote_id)
        self.assertEqual(result["year"], 2019)

        result = book_search.format_search_result(self.second_edition)
        self.assertEqual(result["title"], "Another Edition")
        self.assertEqual(result["key"], self.second_edition.remote_id)
        self.assertIsNone(result["year"])

    def test_search_result(self):
        """a class that stores info about a search result"""
        models.Connector.objects.create(
            identifier="example.com",
            connector_file="openlibrary",
            base_url="https://example.com",
            books_url="https://example.com/books",
            covers_url="https://example.com/covers",
            search_url="https://example.com/search?q=",
            isbn_search_url="https://example.com/isbn?q=",
        )

        class TestConnector(AbstractMinimalConnector):
            """nothing added here"""

            def get_or_create_book(self, remote_id):
                pass

            def parse_search_data(self, data, min_confidence):
                return data

            def parse_isbn_search_data(self, data):
                return data

        test_connector = TestConnector("example.com")
        result = book_search.SearchResult(
            title="Title",
            key="https://example.com/book/1",
            author="Author Name",
            year="1850",
            connector=test_connector,
        )
        # there's really not much to test here, it's just a dataclass
        self.assertEqual(result.confidence, 1)
        self.assertEqual(result.title, "Title")


class SearchVectorTest(TestCase):
    """check search_vector is computed correctly"""

    def test_search_vector_simple(self):
        """simplest search vector"""
        book = self._create_book("Book", "Mary")
        self.assertEqual(book.search_vector, "'book':1A 'mary':2C")  # A > C (priority)

    def test_search_vector_all_parts(self):
        """search vector with subtitle and series"""
        # for a book like this we call `to_tsvector("Book Long Mary Bunch")`, hence the
        # indexes in the search vector. (priority "D" is the default, and never shown.)
        book = self._create_book("Book", "Mary", subtitle="Long", series="Bunch")
        self.assertEqual(book.search_vector, "'book':1A 'bunch':4 'long':2B 'mary':3C")

    def test_search_vector_parse_book(self):
        """book parts are parsed in english"""
        # FIXME: at some point this should stop being the default.
        book = self._create_book(
            "Edition", "Editor", series="Castle", subtitle="Writing"
        )
        self.assertEqual(
            book.search_vector, "'castl':4 'edit':1A 'editor':3C 'write':2B"
        )

    def test_search_vector_parse_author(self):
        """author name is not stem'd or affected by stop words"""
        book = self._create_book("Writing", "Writes")
        self.assertEqual(book.search_vector, "'write':1A 'writes':2C")

        book = self._create_book("She Is Writing", "She Writes")
        self.assertEqual(book.search_vector, "'she':4C 'write':3A 'writes':5C")

    def test_search_vector_parse_title_empty(self):
        """empty parse in English retried as simple title"""
        book = self._create_book("Here We", "John")
        self.assertEqual(book.search_vector, "'here':1A 'john':3C 'we':2A")

        book = self._create_book("Hear We Come", "John")
        self.assertEqual(book.search_vector, "'come':3A 'hear':1A 'john':4C")

    @staticmethod
    def _create_book(
        title, author_name, /, *, subtitle="", series="", author_alias=None
    ):
        """quickly create a book"""
        work = models.Work.objects.create(title="work")
        author = models.Author.objects.create(
            name=author_name, aliases=author_alias or []
        )
        edition = models.Edition.objects.create(
            title=title,
            series=series or None,
            subtitle=subtitle or None,
            isbn_10="0000000000",
            parent_work=work,
        )
        edition.authors.add(author)
        edition.save(broadcast=False)
        edition.refresh_from_db()
        return edition


class SearchVectorTriggers(TestCase):
    """look for books as they change"""

    def setUp(self):
        """we need basic test data and mocks"""
        self.work = models.Work.objects.create(title="This Work")
        self.author = models.Author.objects.create(name="Name")
        self.edition = models.Edition.objects.create(
            title="First Edition of Work",
            subtitle="Some Extra Words Are Good",
            series="A Fabulous Sequence of Items",
            parent_work=self.work,
            isbn_10="0000000000",
        )
        self.edition.authors.add(self.author)
        self.edition.save(broadcast=False)

    @classmethod
    def setUpTestData(cls):
        """create conditions that trigger known old bugs"""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                ALTER SEQUENCE bookwyrm_author_id_seq       RESTART WITH 20;
                ALTER SEQUENCE bookwyrm_book_authors_id_seq RESTART WITH 300;
                """
            )

    def test_search_after_changed_metadata(self):
        """book found after updating metadata"""
        self.assertEqual(self.edition, self._search_first("First"))  # title
        self.assertEqual(self.edition, self._search_first("Good"))  # subtitle
        self.assertEqual(self.edition, self._search_first("Sequence"))  # series

        self.edition.title = "Second Title of Work"
        self.edition.subtitle = "Fewer Words Is Better"
        self.edition.series = "A Wondrous Bunch"
        self.edition.save(broadcast=False)

        self.assertEqual(self.edition, self._search_first("Second"))  # title new
        self.assertEqual(self.edition, self._search_first("Fewer"))  # subtitle new
        self.assertEqual(self.edition, self._search_first("Wondrous"))  # series new

        self.assertFalse(self._search_first("First"))  # title old
        self.assertFalse(self._search_first("Good"))  # subtitle old
        self.assertFalse(self._search_first("Sequence"))  # series old

    def test_search_after_author_remove(self):
        """book not found via removed author"""
        self.assertEqual(self.edition, self._search_first("Name"))

        self.edition.authors.set([])
        self.edition.save(broadcast=False)

        self.assertFalse(self._search("Name"))
        self.assertEqual(self.edition, self._search_first("Edition"))

    def test_search_after_author_add(self):
        """book found by newly-added author"""
        new_author = models.Author.objects.create(name="Mozilla")

        self.assertFalse(self._search("Mozilla"))

        self.edition.authors.add(new_author)
        self.edition.save(broadcast=False)

        self.assertEqual(self.edition, self._search_first("Mozilla"))
        self.assertEqual(self.edition, self._search_first("Name"))

    def test_search_after_updated_author_name(self):
        """book found under new author name"""
        self.assertEqual(self.edition, self._search_first("Name"))
        self.assertFalse(self._search("Identifier"))

        self.author.name = "Identifier"
        self.author.save(broadcast=False)

        self.assertFalse(self._search("Name"))
        self.assertEqual(self.edition, self._search_first("Identifier"))
        self.assertEqual(self.edition, self._search_first("Work"))

    def _search_first(self, query):
        """wrapper around search_title_author"""
        return self._search(query, return_first=True)

    @staticmethod
    def _search(query, *, return_first=False):
        """wrapper around search_title_author"""
        return book_search.search_title_author(
            query, min_confidence=0, return_first=return_first
        )
