"""Export user account to tar.gz file for import into another Bookwyrm instance"""

import logging
from urllib.parse import urlparse, unquote

from boto3.session import Session as BotoSession
from s3_tar import S3Tar
from storages.backends.s3boto3 import S3Boto3Storage

from django.db.models import CASCADE, BooleanField, FileField, ForeignKey, JSONField
from django.db.models import Q
from django.core.serializers.json import DjangoJSONEncoder
from django.core.files.base import ContentFile
from django.utils import timezone

from bookwyrm import settings, storage_backends

from bookwyrm.models import AnnualGoal, ReadThrough, ShelfBook, List, ListItem
from bookwyrm.models import Review, Comment, Quotation
from bookwyrm.models import Edition
from bookwyrm.models import UserFollows, User, UserBlocks
from bookwyrm.models.job import ParentJob, ChildJob, ParentTask
from bookwyrm.tasks import app, IMPORTS
from bookwyrm.utils.tar import BookwyrmTarFile

logger = logging.getLogger(__name__)


class BookwyrmAwsSession(BotoSession):
    """a boto session that always uses settings.AWS_S3_ENDPOINT_URL"""

    def client(self, *args, **kwargs):  # pylint: disable=arguments-differ
        kwargs["endpoint_url"] = settings.AWS_S3_ENDPOINT_URL
        return super().client("s3", *args, **kwargs)


class BookwyrmExportJob(ParentJob):
    """entry for a specific request to export a bookwyrm user"""

    # Only one of these fields is used, dependent on the configuration.
    export_data_file = FileField(null=True, storage=storage_backends.ExportsFileStorage)
    export_data_s3 = FileField(null=True, storage=storage_backends.ExportsS3Storage)

    export_json = JSONField(null=True, encoder=DjangoJSONEncoder)
    json_completed = BooleanField(default=False)

    @property
    def export_data(self):
        """returns the file field of the configured storage backend"""
        # TODO: We could check whether a field for a different backend is
        # filled, to support migrating to a different backend.
        if settings.USE_S3:
            return self.export_data_s3
        return self.export_data_file

    @export_data.setter
    def export_data(self, value):
        """sets the file field of the configured storage backend"""
        if settings.USE_S3:
            self.export_data_s3 = value
        else:
            self.export_data_file = value

    def start_job(self):
        """Start the job"""

        task = start_export_task.delay(job_id=self.id, no_children=False)
        self.task_id = task.id
        self.save(update_fields=["task_id"])

    def notify_child_job_complete(self):
        """let the job know when the items get work done"""

        if self.complete:
            return

        self.updated_date = timezone.now()
        self.save(update_fields=["updated_date"])

        if not self.complete and self.has_completed:
            if not self.json_completed:
                try:
                    self.json_completed = True
                    self.save(update_fields=["json_completed"])

                    tar_job = AddFileToTar.objects.create(
                        parent_job=self, parent_export_job=self
                    )
                    tar_job.start_job()

                except Exception as err:  # pylint: disable=broad-except
                    logger.exception("job %s failed with error: %s", self.id, err)
                    tar_job.set_status("failed")
                    self.stop_job(reason="failed")

            else:
                self.complete_job()


def url2relativepath(url: str) -> str:
    """turn an absolute URL into a relative filesystem path"""
    parsed = urlparse(url)
    return unquote(parsed.path[1:])


class AddBookToUserExportJob(ChildJob):
    """append book metadata for each book in an export"""

    edition = ForeignKey(Edition, on_delete=CASCADE)

    # pylint: disable=too-many-locals
    def start_job(self):
        """Start the job"""
        try:

            book = {}
            book["work"] = self.edition.parent_work.to_activity()
            book["edition"] = self.edition.to_activity()

            if book["edition"].get("cover"):
                book["edition"]["cover"]["url"] = url2relativepath(
                    book["edition"]["cover"]["url"]
                )

            # authors
            book["authors"] = []
            for author in self.edition.authors.all():
                book["authors"].append(author.to_activity())

            # Shelves this book is on
            # Every ShelfItem is this book so we don't other serializing
            book["shelves"] = []
            shelf_books = (
                ShelfBook.objects.select_related("shelf")
                .filter(user=self.parent_job.user, book=self.edition)
                .distinct()
            )

            for shelfbook in shelf_books:
                book["shelves"].append(shelfbook.shelf.to_activity())

            # Lists and ListItems
            # ListItems include "notes" and "approved" so we need them
            # even though we know it's this book
            book["lists"] = []
            list_items = ListItem.objects.filter(
                book=self.edition, user=self.parent_job.user
            ).distinct()

            for item in list_items:
                list_info = item.book_list.to_activity()
                list_info[
                    "privacy"
                ] = item.book_list.privacy  # this isn't serialized so we add it
                list_info["list_item"] = item.to_activity()
                book["lists"].append(list_info)

            # Statuses
            # Can't use select_subclasses here because
            # we need to filter on the "book" value,
            # which is not available on an ordinary Status
            for status in ["comments", "quotations", "reviews"]:
                book[status] = []

            comments = Comment.objects.filter(
                user=self.parent_job.user, book=self.edition
            ).all()
            for status in comments:
                obj = status.to_activity()
                obj["progress"] = status.progress
                obj["progress_mode"] = status.progress_mode
                book["comments"].append(obj)

            quotes = Quotation.objects.filter(
                user=self.parent_job.user, book=self.edition
            ).all()
            for status in quotes:
                obj = status.to_activity()
                obj["position"] = status.position
                obj["endposition"] = status.endposition
                obj["position_mode"] = status.position_mode
                book["quotations"].append(obj)

            reviews = Review.objects.filter(
                user=self.parent_job.user, book=self.edition
            ).all()
            for status in reviews:
                obj = status.to_activity()
                book["reviews"].append(obj)

            # readthroughs can't be serialized to activity
            book_readthroughs = (
                ReadThrough.objects.filter(user=self.parent_job.user, book=self.edition)
                .distinct()
                .values()
            )
            book["readthroughs"] = list(book_readthroughs)

            self.parent_job.export_json["books"].append(book)
            self.parent_job.save(update_fields=["export_json"])
            self.complete_job()

        except Exception as err:  # pylint: disable=broad-except
            logger.exception(
                "AddBookToUserExportJob %s Failed with error: %s", self.id, err
            )
            self.set_status("failed")


class AddFileToTar(ChildJob):
    """add files to export"""

    parent_export_job = ForeignKey(
        BookwyrmExportJob, on_delete=CASCADE, related_name="child_edition_export_jobs"
    )

    def start_job(self):
        """Start the job"""

        # NOTE we are doing this all in one big job,
        # which has the potential to block a thread
        # This is because we need to refer to the same s3_job
        # or BookwyrmTarFile whilst writing
        # Using a series of jobs in a loop would be better

        try:
            export_job = self.parent_export_job
            export_task_id = str(export_job.task_id)

            export_json_bytes = (
                DjangoJSONEncoder().encode(export_job.export_json).encode("utf-8")
            )

            user = export_job.user
            editions = get_books_for_user(user)

            if settings.USE_S3:
                # Connection for writing temporary files
                s3 = S3Boto3Storage()

                # Handle for creating the final archive
                s3_archive_path = f"exports/{export_task_id}.tar.gz"
                s3_tar = S3Tar(
                    settings.AWS_STORAGE_BUCKET_NAME,
                    s3_archive_path,
                    session=BookwyrmAwsSession(),
                )

                # Save JSON file to a temporary location
                export_json_tmp_file = f"exports/{export_task_id}/archive.json"
                S3Boto3Storage.save(
                    s3,
                    export_json_tmp_file,
                    ContentFile(export_json_bytes),
                )
                s3_tar.add_file(export_json_tmp_file)

                # Add avatar image if present
                if user.avatar:
                    s3_tar.add_file(f"images/{user.avatar.name}")

                for edition in editions:
                    if edition.cover:
                        s3_tar.add_file(f"images/{edition.cover.name}")

                # Create archive and store file name
                s3_tar.tar()
                export_job.export_data_s3 = s3_archive_path
                export_job.save()

                # Delete temporary files
                S3Boto3Storage.delete(s3, export_json_tmp_file)

            else:
                export_job.export_data_file = f"{export_task_id}.tar.gz"
                with export_job.export_data_file.open("wb") as f:
                    with BookwyrmTarFile.open(mode="w:gz", fileobj=f) as tar:
                        # save json file
                        tar.write_bytes(export_json_bytes)

                        # Add avatar image if present
                        if user.avatar:
                            tar.add_image(user.avatar, directory="images/")

                        for edition in editions:
                            if edition.cover:
                                tar.add_image(edition.cover, directory="images/")
                export_job.save()

            self.complete_job()

        except Exception as err:  # pylint: disable=broad-except
            logger.exception("AddFileToTar %s Failed with error: %s", self.id, err)
            self.stop_job(reason="failed")
            self.parent_job.stop_job(reason="failed")


@app.task(queue=IMPORTS, base=ParentTask)
def start_export_task(**kwargs):
    """trigger the child tasks for user export"""

    job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])

    # don't start the job if it was stopped from the UI
    if job.complete:
        return
    try:

        # prepare the initial file and base json
        job.export_json = job.user.to_activity()
        job.save(update_fields=["export_json"])

        # let's go
        json_export.delay(job_id=job.id, job_user=job.user.id, no_children=False)

    except Exception as err:  # pylint: disable=broad-except
        logger.exception("User Export Job %s Failed with error: %s", job.id, err)
        job.set_status("failed")


@app.task(queue=IMPORTS, base=ParentTask)
def export_saved_lists_task(**kwargs):
    """add user saved lists to export JSON"""

    job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])
    saved_lists = List.objects.filter(id__in=job.user.saved_lists.all()).distinct()
    job.export_json["saved_lists"] = [l.remote_id for l in saved_lists]
    job.save(update_fields=["export_json"])


@app.task(queue=IMPORTS, base=ParentTask)
def export_follows_task(**kwargs):
    """add user follows to export JSON"""

    job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])
    follows = UserFollows.objects.filter(user_subject=job.user).distinct()
    following = User.objects.filter(userfollows_user_object__in=follows).distinct()
    job.export_json["follows"] = [f.remote_id for f in following]
    job.save(update_fields=["export_json"])


@app.task(queue=IMPORTS, base=ParentTask)
def export_blocks_task(**kwargs):
    """add user blocks to export JSON"""

    job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])
    blocks = UserBlocks.objects.filter(user_subject=job.user).distinct()
    blocking = User.objects.filter(userblocks_user_object__in=blocks).distinct()
    job.export_json["blocks"] = [b.remote_id for b in blocking]
    job.save(update_fields=["export_json"])


@app.task(queue=IMPORTS, base=ParentTask)
def export_reading_goals_task(**kwargs):
    """add user reading goals to export JSON"""

    job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])
    reading_goals = AnnualGoal.objects.filter(user=job.user).distinct()
    job.export_json["goals"] = []
    for goal in reading_goals:
        job.export_json["goals"].append(
            {"goal": goal.goal, "year": goal.year, "privacy": goal.privacy}
        )
    job.save(update_fields=["export_json"])


@app.task(queue=IMPORTS, base=ParentTask)
def json_export(**kwargs):
    """Generate an export for a user"""

    try:
        job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])
        job.set_status("active")
        job_id = kwargs["job_id"]

        if not job.export_json.get("icon"):
            job.export_json["icon"] = {}
        else:
            job.export_json["icon"]["url"] = url2relativepath(
                job.export_json["icon"]["url"]
            )

        # Additional settings - can't be serialized as AP
        vals = [
            "show_goal",
            "preferred_timezone",
            "default_post_privacy",
            "show_suggested_users",
        ]
        job.export_json["settings"] = {}
        for k in vals:
            job.export_json["settings"][k] = getattr(job.user, k)

        job.export_json["books"] = []

        # save settings we just updated
        job.save(update_fields=["export_json"])

        # trigger subtasks
        export_saved_lists_task.delay(job_id=job_id, no_children=False)
        export_follows_task.delay(job_id=job_id, no_children=False)
        export_blocks_task.delay(job_id=job_id, no_children=False)
        trigger_books_jobs.delay(job_id=job_id, no_children=False)

    except Exception as err:  # pylint: disable=broad-except
        logger.exception(
            "json_export task in job %s Failed with error: %s",
            job.id,
            err,
        )
        job.set_status("failed")


@app.task(queue=IMPORTS, base=ParentTask)
def trigger_books_jobs(**kwargs):
    """trigger tasks to get data for each book"""

    try:
        job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])
        editions = get_books_for_user(job.user)

        if len(editions) == 0:
            job.notify_child_job_complete()
            return

        for edition in editions:
            try:
                edition_job = AddBookToUserExportJob.objects.create(
                    edition=edition, parent_job=job
                )
                edition_job.start_job()
            except Exception as err:  # pylint: disable=broad-except
                logger.exception(
                    "AddBookToUserExportJob %s Failed with error: %s",
                    edition_job.id,
                    err,
                )
                edition_job.set_status("failed")

    except Exception as err:  # pylint: disable=broad-except
        logger.exception("trigger_books_jobs %s Failed with error: %s", job.id, err)
        job.set_status("failed")


def get_books_for_user(user):
    """Get all the books and editions related to a user"""

    editions = (
        Edition.objects.select_related("parent_work")
        .filter(
            Q(shelves__user=user)
            | Q(readthrough__user=user)
            | Q(review__user=user)
            | Q(list__user=user)
            | Q(comment__user=user)
            | Q(quotation__user=user)
        )
        .distinct()
    )

    return editions
