# Copyright 2020 The HuggingFace Datasets Authors and the TensorFlow Datasets Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Access datasets."""

import glob
import importlib
import inspect
import json
import os
import posixpath
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import fsspec
import requests
import yaml
from fsspec.core import url_to_fs
from huggingface_hub import DatasetCard, DatasetCardData, HfApi
from huggingface_hub.utils import (
    EntryNotFoundError,
    GatedRepoError,
    LocalEntryNotFoundError,
    OfflineModeIsEnabled,
    RepositoryNotFoundError,
    RevisionNotFoundError,
    get_session,
)

from . import __version__, config
from .arrow_dataset import Dataset
from .builder import BuilderConfig, DatasetBuilder
from .data_files import (
    DataFilesDict,
    DataFilesList,
    DataFilesPatternsDict,
    EmptyDatasetError,
    get_data_patterns,
    sanitize_patterns,
)
from .dataset_dict import DatasetDict, IterableDatasetDict
from .download.download_config import DownloadConfig
from .download.download_manager import DownloadMode
from .download.streaming_download_manager import StreamingDownloadManager, xbasename, xglob, xjoin
from .exceptions import DataFilesNotFoundError, DatasetNotFoundError
from .features import Features
from .fingerprint import Hasher
from .info import DatasetInfo, DatasetInfosDict
from .iterable_dataset import IterableDataset
from .naming import camelcase_to_snakecase, snakecase_to_camelcase
from .packaged_modules import (
    _EXTENSION_TO_MODULE,
    _MODULE_TO_EXTENSIONS,
    _MODULE_TO_METADATA_FILE_NAMES,
    _PACKAGED_DATASETS_MODULES,
)
from .packaged_modules.folder_based_builder.folder_based_builder import FolderBasedBuilder
from .splits import Split
from .utils import _dataset_viewer
from .utils.file_utils import (
    _raise_if_offline_mode_is_enabled,
    cached_path,
    get_datasets_user_agent,
    is_relative_path,
    relative_to_absolute_path,
)
from .utils.hub import hf_dataset_url
from .utils.info_utils import VerificationMode, is_small_dataset
from .utils.logging import get_logger
from .utils.metadata import MetadataConfigs
from .utils.typing import PathLike
from .utils.version import Version


logger = get_logger(__name__)

ALL_ALLOWED_EXTENSIONS = list(_EXTENSION_TO_MODULE.keys()) + [".zip"]


class _InitializeConfiguredDatasetBuilder:
    """
    From https://stackoverflow.com/questions/4647566/pickle-a-dynamically-parameterized-sub-class
    See also ConfiguredDatasetBuilder.__reduce__
    When called with the param value as the only argument, returns an
    un-initialized instance of the parameterized class. Subsequent __setstate__
    will be called by pickle.
    """

    def __call__(self, builder_cls, metadata_configs, default_config_name, name):
        # make a simple object which has no complex __init__ (this one will do)
        obj = _InitializeConfiguredDatasetBuilder()
        obj.__class__ = configure_builder_class(
            builder_cls, metadata_configs, default_config_name=default_config_name, dataset_name=name
        )
        return obj


def configure_builder_class(
    builder_cls: type[DatasetBuilder],
    builder_configs: list[BuilderConfig],
    default_config_name: Optional[str],
    dataset_name: str,
) -> type[DatasetBuilder]:
    """
    Dynamically create a builder class with custom builder configs parsed from README.md file,
    i.e. set BUILDER_CONFIGS class variable of a builder class to custom configs list.
    """

    class ConfiguredDatasetBuilder(builder_cls):
        BUILDER_CONFIGS = builder_configs
        DEFAULT_CONFIG_NAME = default_config_name

        __module__ = builder_cls.__module__  # so that the actual packaged builder can be imported

        def __reduce__(self):  # to make dynamically created class pickable, see _InitializeParameterizedDatasetBuilder
            parent_builder_cls = self.__class__.__mro__[1]
            return (
                _InitializeConfiguredDatasetBuilder(),
                (
                    parent_builder_cls,
                    self.BUILDER_CONFIGS,
                    self.DEFAULT_CONFIG_NAME,
                    self.dataset_name,
                ),
                self.__dict__.copy(),
            )

    ConfiguredDatasetBuilder.__name__ = (
        f"{builder_cls.__name__.lower().capitalize()}{snakecase_to_camelcase(dataset_name)}"
    )
    ConfiguredDatasetBuilder.__qualname__ = (
        f"{builder_cls.__name__.lower().capitalize()}{snakecase_to_camelcase(dataset_name)}"
    )

    return ConfiguredDatasetBuilder


def import_main_class(module_path) -> Optional[type[DatasetBuilder]]:
    """Import a module at module_path and return its main class: a DatasetBuilder"""
    module = importlib.import_module(module_path)
    # Find the main class in our imported module
    module_main_cls = None
    for name, obj in module.__dict__.items():
        if inspect.isclass(obj) and issubclass(obj, DatasetBuilder):
            if inspect.isabstract(obj):
                continue
            module_main_cls = obj
            obj_module = inspect.getmodule(obj)
            if obj_module is not None and module == obj_module:
                break

    return module_main_cls


def get_dataset_builder_class(
    dataset_module: "DatasetModule", dataset_name: Optional[str] = None
) -> type[DatasetBuilder]:
    with (
        lock_importable_file(dataset_module.importable_file_path)
        if dataset_module.importable_file_path
        else nullcontext()
    ):
        builder_cls = import_main_class(dataset_module.module_path)
    if dataset_module.builder_configs_parameters.builder_configs:
        dataset_name = dataset_name or dataset_module.builder_kwargs.get("dataset_name")
        if dataset_name is None:
            raise ValueError("dataset_name should be specified but got None")
        builder_cls = configure_builder_class(
            builder_cls,
            builder_configs=dataset_module.builder_configs_parameters.builder_configs,
            default_config_name=dataset_module.builder_configs_parameters.default_config_name,
            dataset_name=dataset_name,
        )
    return builder_cls


def increase_load_count(name: str):
    """Update the download count of a dataset."""
    if not config.HF_HUB_OFFLINE and config.HF_UPDATE_DOWNLOAD_COUNTS:
        try:
            get_session().head(
                "/".join((config.S3_DATASETS_BUCKET_PREFIX, name, name + ".py")),
                headers={"User-Agent": get_datasets_user_agent()},
                timeout=3,
            )
        except Exception:
            pass


def infer_module_for_data_files_list(
    data_files_list: DataFilesList, download_config: Optional[DownloadConfig] = None
) -> tuple[Optional[str], dict]:
    """Infer module (and builder kwargs) from list of data files.

    It picks the module based on the most common file extension.
    In case of a draw ".parquet" is the favorite, and then alphabetical order.

    Args:
        data_files_list (DataFilesList): List of data files.
        download_config (bool or str, optional): Mainly use `token` or `storage_options` to support different platforms and auth types.

    Returns:
        tuple[str, dict[str, Any]]: Tuple with
            - inferred module name
            - dict of builder kwargs
    """
    extensions_counter = Counter(
        ("." + suffix.lower(), xbasename(filepath) in FolderBasedBuilder.METADATA_FILENAMES)
        for filepath in data_files_list[: config.DATA_FILES_MAX_NUMBER_FOR_MODULE_INFERENCE]
        for suffix in xbasename(filepath).split(".")[1:]
    )
    if extensions_counter:

        def sort_key(ext_count: tuple[tuple[str, bool], int]) -> tuple[int, bool]:
            """Sort by count and set ".parquet" as the favorite in case of a draw, and ignore metadata files"""
            (ext, is_metadata), count = ext_count
            return (not is_metadata, count, ext == ".parquet", ext == ".jsonl", ext == ".json", ext == ".csv", ext)

        for (ext, _), _ in sorted(extensions_counter.items(), key=sort_key, reverse=True):
            if ext in _EXTENSION_TO_MODULE:
                return _EXTENSION_TO_MODULE[ext]
            elif ext == ".zip":
                return infer_module_for_data_files_list_in_archives(data_files_list, download_config=download_config)
    return None, {}


def infer_module_for_data_files_list_in_archives(
    data_files_list: DataFilesList, download_config: Optional[DownloadConfig] = None
) -> tuple[Optional[str], dict]:
    """Infer module (and builder kwargs) from list of archive data files.

    Args:
        data_files_list (DataFilesList): List of data files.
        download_config (bool or str, optional): Mainly use `token` or `storage_options` to support different platforms and auth types.

    Returns:
        tuple[str, dict[str, Any]]: Tuple with
            - inferred module name
            - dict of builder kwargs
    """
    archived_files = []
    archive_files_counter = 0
    for filepath in data_files_list:
        if str(filepath).endswith(".zip"):
            archive_files_counter += 1
            if archive_files_counter > config.GLOBBED_DATA_FILES_MAX_NUMBER_FOR_MODULE_INFERENCE:
                break
            extracted = xjoin(StreamingDownloadManager().extract(filepath), "**")
            archived_files += [
                f.split("::")[0]
                for f in xglob(extracted, recursive=True, download_config=download_config)[
                    : config.ARCHIVED_DATA_FILES_MAX_NUMBER_FOR_MODULE_INFERENCE
                ]
            ]
    extensions_counter = Counter(
        "." + suffix.lower() for filepath in archived_files for suffix in xbasename(filepath).split(".")[1:]
    )
    if extensions_counter:
        most_common = extensions_counter.most_common(1)[0][0]
        if most_common in _EXTENSION_TO_MODULE:
            return _EXTENSION_TO_MODULE[most_common]
    return None, {}


def infer_module_for_data_files(
    data_files: DataFilesDict, path: Optional[str] = None, download_config: Optional[DownloadConfig] = None
) -> tuple[Optional[str], dict[str, Any]]:
    """Infer module (and builder kwargs) from data files. Raise if module names for different splits don't match.

    Args:
        data_files ([`DataFilesDict`]): Dict of list of data files.
        path (str, *optional*): Dataset name or path.
        download_config ([`DownloadConfig`], *optional*):
            Specific download configuration parameters to authenticate on the Hugging Face Hub for private remote files.

    Returns:
        tuple[str, dict[str, Any]]: Tuple with
            - inferred module name
            - builder kwargs
    """
    split_modules = {
        split: infer_module_for_data_files_list(data_files_list, download_config=download_config)
        for split, data_files_list in data_files.items()
    }
    module_name, default_builder_kwargs = next(iter(split_modules.values()))
    if any((module_name, default_builder_kwargs) != split_module for split_module in split_modules.values()):
        raise ValueError(f"Couldn't infer the same data file format for all splits. Got {split_modules}")
    if not module_name:
        raise DataFilesNotFoundError("No (supported) data files found" + (f" in {path}" if path else ""))
    return module_name, default_builder_kwargs


def create_builder_configs_from_metadata_configs(
    module_path: str,
    metadata_configs: MetadataConfigs,
    base_path: Optional[str] = None,
    default_builder_kwargs: dict[str, Any] = None,
    download_config: Optional[DownloadConfig] = None,
) -> tuple[list[BuilderConfig], str]:
    builder_cls = import_main_class(module_path)
    builder_config_cls = builder_cls.BUILDER_CONFIG_CLASS
    default_config_name = metadata_configs.get_default_config_name()
    builder_configs = []
    default_builder_kwargs = {} if default_builder_kwargs is None else default_builder_kwargs

    base_path = base_path if base_path is not None else ""
    for config_name, config_params in metadata_configs.items():
        config_data_files = config_params.get("data_files")
        config_data_dir = config_params.get("data_dir")
        config_base_path = xjoin(base_path, config_data_dir) if config_data_dir else base_path
        try:
            config_patterns = (
                sanitize_patterns(config_data_files)
                if config_data_files is not None
                else get_data_patterns(config_base_path, download_config=download_config)
            )
            config_data_files_dict = DataFilesPatternsDict.from_patterns(
                config_patterns,
                allowed_extensions=ALL_ALLOWED_EXTENSIONS,
            )
        except EmptyDatasetError as e:
            raise EmptyDatasetError(
                f"Dataset at '{base_path}' doesn't contain data files matching the patterns for config '{config_name}',"
                f" check `data_files` and `data_fir` parameters in the `configs` YAML field in README.md. "
            ) from e
        ignored_params = [
            param for param in config_params if not hasattr(builder_config_cls, param) and param != "default"
        ]
        if ignored_params:
            logger.warning(
                f"Some datasets params were ignored: {ignored_params}. "
                "Make sure to use only valid params for the dataset builder and to have "
                "a up-to-date version of the `datasets` library."
            )
        builder_configs.append(
            builder_config_cls(
                name=config_name,
                data_files=config_data_files_dict,
                data_dir=config_data_dir,
                **{
                    param: value
                    for param, value in {**default_builder_kwargs, **config_params}.items()
                    if hasattr(builder_config_cls, param) and param not in ("default", "data_files", "data_dir")
                },
            )
        )
    return builder_configs, default_config_name


@dataclass
class BuilderConfigsParameters:
    """Dataclass containing objects related to creation of builder configurations from yaml's metadata content.

    Attributes:
        metadata_configs (`MetadataConfigs`, *optional*):
            Configs parsed from yaml's metadata.
        builder_configs (`list[BuilderConfig]`, *optional*):
            List of BuilderConfig objects created from metadata_configs above.
        default_config_name (`str`):
            Name of default config taken from yaml's metadata.
    """

    metadata_configs: Optional[MetadataConfigs] = None
    builder_configs: Optional[list[BuilderConfig]] = None
    default_config_name: Optional[str] = None


@dataclass
class DatasetModule:
    module_path: str
    hash: str
    builder_kwargs: dict
    builder_configs_parameters: BuilderConfigsParameters = field(default_factory=BuilderConfigsParameters)
    dataset_infos: Optional[DatasetInfosDict] = None
    importable_file_path: Optional[str] = None


class _DatasetModuleFactory:
    def get_module(self) -> DatasetModule:
        raise NotImplementedError


class LocalDatasetModuleFactory(_DatasetModuleFactory):
    """Get the module of a dataset loaded from the user's data files. The dataset builder module to use is inferred
    from the data files extensions."""

    def __init__(
        self,
        path: str,
        data_dir: Optional[str] = None,
        data_files: Optional[Union[str, list, dict]] = None,
        download_mode: Optional[Union[DownloadMode, str]] = None,
    ):
        if data_dir and os.path.isabs(data_dir):
            raise ValueError(f"`data_dir` must be relative to a dataset directory's root: {path}")

        self.path = Path(path).as_posix()
        self.name = Path(path).stem
        self.data_files = data_files
        self.data_dir = data_dir
        self.download_mode = download_mode

    def get_module(self) -> DatasetModule:
        readme_path = os.path.join(self.path, config.REPOCARD_FILENAME)
        standalone_yaml_path = os.path.join(self.path, config.REPOYAML_FILENAME)
        dataset_card_data = DatasetCard.load(readme_path).data if os.path.isfile(readme_path) else DatasetCardData()
        if os.path.exists(standalone_yaml_path):
            with open(standalone_yaml_path, encoding="utf-8") as f:
                standalone_yaml_data = yaml.safe_load(f.read())
                if standalone_yaml_data:
                    _dataset_card_data_dict = dataset_card_data.to_dict()
                    _dataset_card_data_dict.update(standalone_yaml_data)
                    dataset_card_data = DatasetCardData(**_dataset_card_data_dict)
        metadata_configs = MetadataConfigs.from_dataset_card_data(dataset_card_data)
        dataset_infos = DatasetInfosDict.from_dataset_card_data(dataset_card_data)
        # we need a set of data files to find which dataset builder to use
        # because we need to infer module name by files extensions
        base_path = Path(self.path, self.data_dir or "").expanduser().resolve().as_posix()
        if self.data_files is not None:
            patterns = sanitize_patterns(self.data_files)
        elif metadata_configs and not self.data_dir and "data_files" in next(iter(metadata_configs.values())):
            patterns = sanitize_patterns(next(iter(metadata_configs.values()))["data_files"])
        else:
            patterns = get_data_patterns(base_path)
        data_files = DataFilesDict.from_patterns(
            patterns,
            base_path=base_path,
            allowed_extensions=ALL_ALLOWED_EXTENSIONS,
        )
        module_name, default_builder_kwargs = infer_module_for_data_files(
            data_files=data_files,
            path=self.path,
        )
        data_files = data_files.filter(
            extensions=_MODULE_TO_EXTENSIONS[module_name], file_names=_MODULE_TO_METADATA_FILE_NAMES[module_name]
        )
        module_path, _ = _PACKAGED_DATASETS_MODULES[module_name]
        if metadata_configs:
            builder_configs, default_config_name = create_builder_configs_from_metadata_configs(
                module_path,
                metadata_configs,
                base_path=base_path,
                default_builder_kwargs=default_builder_kwargs,
            )
        else:
            builder_configs: list[BuilderConfig] = [
                import_main_class(module_path).BUILDER_CONFIG_CLASS(
                    data_files=data_files,
                    **default_builder_kwargs,
                )
            ]
            default_config_name = None
        builder_kwargs = {
            "base_path": self.path,
            "dataset_name": camelcase_to_snakecase(Path(self.path).name),
        }
        if self.data_dir:
            builder_kwargs["data_files"] = data_files
        # this file is deprecated and was created automatically in old versions of push_to_hub
        if os.path.isfile(os.path.join(self.path, config.DATASETDICT_INFOS_FILENAME)):
            with open(os.path.join(self.path, config.DATASETDICT_INFOS_FILENAME), encoding="utf-8") as f:
                legacy_dataset_infos = DatasetInfosDict(
                    {
                        config_name: DatasetInfo.from_dict(dataset_info_dict)
                        for config_name, dataset_info_dict in json.load(f).items()
                    }
                )
                if len(legacy_dataset_infos) == 1:
                    # old config e.g. named "username--dataset_name"
                    legacy_config_name = next(iter(legacy_dataset_infos))
                    legacy_dataset_infos["default"] = legacy_dataset_infos.pop(legacy_config_name)
            legacy_dataset_infos.update(dataset_infos)
            dataset_infos = legacy_dataset_infos
        if default_config_name is None and len(dataset_infos) == 1:
            default_config_name = next(iter(dataset_infos))

        hash = Hasher.hash({"dataset_infos": dataset_infos, "builder_configs": builder_configs})
        return DatasetModule(
            module_path,
            hash,
            builder_kwargs,
            dataset_infos=dataset_infos,
            builder_configs_parameters=BuilderConfigsParameters(
                metadata_configs=metadata_configs,
                builder_configs=builder_configs,
                default_config_name=default_config_name,
            ),
        )


class PackagedDatasetModuleFactory(_DatasetModuleFactory):
    """Get the dataset builder module from the ones that are packaged with the library: csv, json, etc."""

    def __init__(
        self,
        name: str,
        data_dir: Optional[str] = None,
        data_files: Optional[Union[str, list, dict]] = None,
        download_config: Optional[DownloadConfig] = None,
        download_mode: Optional[Union[DownloadMode, str]] = None,
    ):
        self.name = name
        self.data_files = data_files
        self.data_dir = data_dir
        self.download_config = download_config
        self.download_mode = download_mode
        increase_load_count(name)

    def get_module(self) -> DatasetModule:
        base_path = Path(self.data_dir or "").expanduser().resolve().as_posix()
        patterns = (
            sanitize_patterns(self.data_files)
            if self.data_files is not None
            else get_data_patterns(base_path, download_config=self.download_config)
        )
        data_files = DataFilesDict.from_patterns(
            patterns,
            download_config=self.download_config,
            base_path=base_path,
        )

        module_path, hash = _PACKAGED_DATASETS_MODULES[self.name]

        builder_kwargs = {
            "data_files": data_files,
            "dataset_name": self.name,
        }

        return DatasetModule(module_path, hash, builder_kwargs)


class HubDatasetModuleFactory(_DatasetModuleFactory):
    """
    Get the module of a dataset loaded from data files of a dataset repository.
    The dataset builder module to use is inferred from the data files extensions.
    """

    def __init__(
        self,
        name: str,
        commit_hash: str,
        data_dir: Optional[str] = None,
        data_files: Optional[Union[str, list, dict]] = None,
        download_config: Optional[DownloadConfig] = None,
        download_mode: Optional[Union[DownloadMode, str]] = None,
        use_exported_dataset_infos: bool = False,
    ):
        self.name = name
        self.commit_hash = commit_hash
        self.data_files = data_files
        self.data_dir = data_dir
        self.download_config = download_config or DownloadConfig()
        self.download_mode = download_mode
        self.use_exported_dataset_infos = use_exported_dataset_infos
        increase_load_count(name)

    def get_module(self) -> DatasetModule:
        # Get the Dataset Card and fix the revision in case there are new commits in the meantime
        api = HfApi(
            endpoint=config.HF_ENDPOINT,
            token=self.download_config.token,
            library_name="datasets",
            library_version=__version__,
            user_agent=get_datasets_user_agent(self.download_config.user_agent),
        )
        try:
            dataset_readme_path = api.hf_hub_download(
                repo_id=self.name,
                filename=config.REPOCARD_FILENAME,
                repo_type="dataset",
                revision=self.commit_hash,
                proxies=self.download_config.proxies,
            )
            dataset_card_data = DatasetCard.load(dataset_readme_path).data
        except EntryNotFoundError:
            dataset_card_data = DatasetCardData()
        download_config = self.download_config.copy()
        if download_config.download_desc is None:
            download_config.download_desc = "Downloading standalone yaml"
        try:
            standalone_yaml_path = cached_path(
                hf_dataset_url(self.name, config.REPOYAML_FILENAME, revision=self.commit_hash),
                download_config=download_config,
            )
            with open(standalone_yaml_path, encoding="utf-8") as f:
                standalone_yaml_data = yaml.safe_load(f.read())
                if standalone_yaml_data:
                    _dataset_card_data_dict = dataset_card_data.to_dict()
                    _dataset_card_data_dict.update(standalone_yaml_data)
                    dataset_card_data = DatasetCardData(**_dataset_card_data_dict)
        except FileNotFoundError:
            pass
        base_path = f"hf://datasets/{self.name}@{self.commit_hash}/{self.data_dir or ''}".rstrip("/")
        metadata_configs = MetadataConfigs.from_dataset_card_data(dataset_card_data)
        dataset_infos = DatasetInfosDict.from_dataset_card_data(dataset_card_data)
        if config.USE_PARQUET_EXPORT and self.use_exported_dataset_infos:
            try:
                exported_dataset_infos = _dataset_viewer.get_exported_dataset_infos(
                    dataset=self.name, commit_hash=self.commit_hash, token=self.download_config.token
                )
                exported_dataset_infos = DatasetInfosDict(
                    {
                        config_name: DatasetInfo.from_dict(exported_dataset_infos[config_name])
                        for config_name in exported_dataset_infos
                    }
                )
            except _dataset_viewer.DatasetViewerError:
                exported_dataset_infos = None
        else:
            exported_dataset_infos = None
        if exported_dataset_infos:
            exported_dataset_infos.update(dataset_infos)
            dataset_infos = exported_dataset_infos
        # we need a set of data files to find which dataset builder to use
        # because we need to infer module name by files extensions
        if self.data_files is not None:
            patterns = sanitize_patterns(self.data_files)
        elif metadata_configs and not self.data_dir and "data_files" in next(iter(metadata_configs.values())):
            patterns = sanitize_patterns(next(iter(metadata_configs.values()))["data_files"])
        else:
            patterns = get_data_patterns(base_path, download_config=self.download_config)
        data_files = DataFilesDict.from_patterns(
            patterns,
            base_path=base_path,
            allowed_extensions=ALL_ALLOWED_EXTENSIONS,
            download_config=self.download_config,
        )
        module_name, default_builder_kwargs = infer_module_for_data_files(
            data_files=data_files,
            path=self.name,
            download_config=self.download_config,
        )
        data_files = data_files.filter(
            extensions=_MODULE_TO_EXTENSIONS[module_name], file_names=_MODULE_TO_METADATA_FILE_NAMES[module_name]
        )
        module_path, _ = _PACKAGED_DATASETS_MODULES[module_name]
        if metadata_configs:
            builder_configs, default_config_name = create_builder_configs_from_metadata_configs(
                module_path,
                metadata_configs,
                base_path=base_path,
                default_builder_kwargs=default_builder_kwargs,
                download_config=self.download_config,
            )
        else:
            builder_configs: list[BuilderConfig] = [
                import_main_class(module_path).BUILDER_CONFIG_CLASS(
                    data_files=data_files,
                    **default_builder_kwargs,
                )
            ]
            default_config_name = None
        builder_kwargs = {
            "base_path": hf_dataset_url(self.name, "", revision=self.commit_hash).rstrip("/"),
            "repo_id": self.name,
            "dataset_name": camelcase_to_snakecase(Path(self.name).name),
        }
        if self.data_dir:
            builder_kwargs["data_files"] = data_files
        download_config = self.download_config.copy()
        if download_config.download_desc is None:
            download_config.download_desc = "Downloading metadata"
        try:
            # this file is deprecated and was created automatically in old versions of push_to_hub
            dataset_infos_path = cached_path(
                hf_dataset_url(self.name, config.DATASETDICT_INFOS_FILENAME, revision=self.commit_hash),
                download_config=download_config,
            )
            with open(dataset_infos_path, encoding="utf-8") as f:
                legacy_dataset_infos = DatasetInfosDict(
                    {
                        config_name: DatasetInfo.from_dict(dataset_info_dict)
                        for config_name, dataset_info_dict in json.load(f).items()
                    }
                )
                if len(legacy_dataset_infos) == 1:
                    # old config e.g. named "username--dataset_name"
                    legacy_config_name = next(iter(legacy_dataset_infos))
                    legacy_dataset_infos["default"] = legacy_dataset_infos.pop(legacy_config_name)
            legacy_dataset_infos.update(dataset_infos)
            dataset_infos = legacy_dataset_infos
        except FileNotFoundError:
            pass
        if default_config_name is None and len(dataset_infos) == 1:
            default_config_name = next(iter(dataset_infos))

        return DatasetModule(
            module_path,
            self.commit_hash,
            builder_kwargs,
            dataset_infos=dataset_infos,
            builder_configs_parameters=BuilderConfigsParameters(
                metadata_configs=metadata_configs,
                builder_configs=builder_configs,
                default_config_name=default_config_name,
            ),
        )


class HubDatasetModuleFactoryWithParquetExport(_DatasetModuleFactory):
    """
    Get the module of a dataset loaded from parquet files of a dataset repository parquet export.
    """

    def __init__(
        self,
        name: str,
        commit_hash: str,
        download_config: Optional[DownloadConfig] = None,
    ):
        self.name = name
        self.commit_hash = commit_hash
        self.download_config = download_config or DownloadConfig()
        increase_load_count(name)

    def get_module(self) -> DatasetModule:
        exported_parquet_files = _dataset_viewer.get_exported_parquet_files(
            dataset=self.name, commit_hash=self.commit_hash, token=self.download_config.token
        )
        exported_dataset_infos = _dataset_viewer.get_exported_dataset_infos(
            dataset=self.name, commit_hash=self.commit_hash, token=self.download_config.token
        )
        dataset_infos = DatasetInfosDict(
            {
                config_name: DatasetInfo.from_dict(exported_dataset_infos[config_name])
                for config_name in exported_dataset_infos
            }
        )
        parquet_commit_hash = (
            HfApi(
                endpoint=config.HF_ENDPOINT,
                token=self.download_config.token,
                library_name="datasets",
                library_version=__version__,
                user_agent=get_datasets_user_agent(self.download_config.user_agent),
            )
            .dataset_info(
                self.name,
                revision="refs/convert/parquet",
                token=self.download_config.token,
                timeout=100.0,
            )
            .sha
        )  # fix the revision in case there are new commits in the meantime
        metadata_configs = MetadataConfigs._from_exported_parquet_files_and_dataset_infos(
            parquet_commit_hash=parquet_commit_hash,
            exported_parquet_files=exported_parquet_files,
            dataset_infos=dataset_infos,
        )
        module_path, _ = _PACKAGED_DATASETS_MODULES["parquet"]
        builder_configs, default_config_name = create_builder_configs_from_metadata_configs(
            module_path,
            metadata_configs,
            download_config=self.download_config,
        )
        builder_kwargs = {
            "repo_id": self.name,
            "dataset_name": camelcase_to_snakecase(Path(self.name).name),
        }

        return DatasetModule(
            module_path,
            self.commit_hash,
            builder_kwargs,
            dataset_infos=dataset_infos,
            builder_configs_parameters=BuilderConfigsParameters(
                metadata_configs=metadata_configs,
                builder_configs=builder_configs,
                default_config_name=default_config_name,
            ),
        )


class CachedDatasetModuleFactory(_DatasetModuleFactory):
    """
    Get the module of a dataset that has been loaded once already and cached.
    """

    def __init__(
        self,
        name: str,
        cache_dir: Optional[str] = None,
    ):
        self.name = name
        self.cache_dir = cache_dir
        assert self.name.count("/") <= 1

    def get_module(self) -> DatasetModule:
        cache_dir = os.path.expanduser(str(self.cache_dir or config.HF_DATASETS_CACHE))
        namespace_and_dataset_name = self.name.split("/")
        namespace_and_dataset_name[-1] = camelcase_to_snakecase(namespace_and_dataset_name[-1])
        cached_relative_path = "___".join(namespace_and_dataset_name)
        cached_datasets_directory_path_root = os.path.join(cache_dir, cached_relative_path)
        cached_directory_paths = [
            cached_directory_path
            for cached_directory_path in glob.glob(os.path.join(cached_datasets_directory_path_root, "*", "*", "*"))
            if os.path.isdir(cached_directory_path)
        ]
        if cached_directory_paths:
            builder_kwargs = {
                "repo_id": self.name,
                "dataset_name": self.name.split("/")[-1],
            }
            warning_msg = f"Using the latest cached version of the dataset since {self.name} couldn't be found on the Hugging Face Hub"
            if config.HF_HUB_OFFLINE:
                warning_msg += " (offline mode is enabled)."
            logger.warning(warning_msg)
            return DatasetModule(
                "datasets.packaged_modules.cache.cache",
                "auto",
                {**builder_kwargs, "version": "auto"},
            )
        raise FileNotFoundError(f"Dataset {self.name} is not cached in {self.cache_dir}")


def dataset_module_factory(
    path: str,
    revision: Optional[Union[str, Version]] = None,
    download_config: Optional[DownloadConfig] = None,
    download_mode: Optional[Union[DownloadMode, str]] = None,
    data_dir: Optional[str] = None,
    data_files: Optional[Union[dict, list, str, DataFilesDict]] = None,
    cache_dir: Optional[str] = None,
    **download_kwargs,
) -> DatasetModule:
    """
    Download/extract/cache a dataset module.

    Dataset codes are cached inside the dynamic modules cache to allow easy import (avoid ugly sys.path tweaks).

    Args:

        path (str): Path or name of the dataset.
            Depending on ``path``, the dataset builder that is used comes from one of the generic dataset builders (JSON, CSV, Parquet, text etc.).

            For local datasets:

            - if ``path`` is a local directory (containing data files only)
              -> load a generic dataset builder (csv, json, text etc.) based on the content of the directory
              e.g. ``'./path/to/directory/with/my/csv/data'``.

            For datasets on the Hugging Face Hub (list all available datasets with ``huggingface_hub.list_datasets()``)

            - if ``path`` is a dataset repository on the HF hub (containing data files only)
              -> load a generic dataset builder (csv, text etc.) based on the content of the repository
              e.g. ``'username/dataset_name'``, a dataset repository on the HF hub containing your data files.

        revision (:class:`~utils.Version` or :obj:`str`, optional): Version of the dataset to load.
            As datasets have their own git repository on the Datasets Hub, the default version "main" corresponds to their "main" branch.
            You can specify a different version than the default "main" by using a commit SHA or a git tag of the dataset repository.
        download_config (:class:`DownloadConfig`, optional): Specific download configuration parameters.
        download_mode (:class:`DownloadMode` or :obj:`str`, default ``REUSE_DATASET_IF_EXISTS``): Download/generate mode.
        data_dir (:obj:`str`, optional): Directory with the data files. Used only if `data_files` is not specified,
            in which case it's equal to pass `os.path.join(data_dir, "**")` as `data_files`.
        data_files (:obj:`Union[Dict, List, str]`, optional): Defining the data_files of the dataset configuration.
        cache_dir (`str`, *optional*):
            Directory to read/write data. Defaults to `"~/.cache/huggingface/datasets"`.

            <Added version="2.16.0"/>

        **download_kwargs (additional keyword arguments): optional attributes for DownloadConfig() which will override
            the attributes in download_config if supplied.

    Returns:
        DatasetModule
    """
    if download_config is None:
        download_config = DownloadConfig(**download_kwargs)
    download_mode = DownloadMode(download_mode or DownloadMode.REUSE_DATASET_IF_EXISTS)
    download_config.extract_compressed_file = True
    download_config.force_extract = True
    download_config.force_download = download_mode == DownloadMode.FORCE_REDOWNLOAD

    filename = list(filter(lambda x: x, path.replace(os.sep, "/").split("/")))[-1]
    if not filename.endswith(".py"):
        filename = filename + ".py"
    combined_path = os.path.join(path, filename)

    # We have several ways to get a dataset builder:
    #
    # - if path is the name of a packaged dataset module
    #   -> use the packaged module (json, csv, etc.)
    #
    # - if os.path.join(path, name) is a local python file
    #   -> use the module from the python file
    # - if path is a local directory (but no python file)
    #   -> use a packaged module (csv, text etc.) based on content of the directory
    #
    # - if path has one "/" and is dataset repository on the HF hub with a python file
    #   -> the module from the python file in the dataset repository
    # - if path has one "/" and is dataset repository on the HF hub without a python file
    #   -> use a packaged module (csv, text etc.) based on content of the repository

    # Try packaged
    if path in _PACKAGED_DATASETS_MODULES:
        return PackagedDatasetModuleFactory(
            path,
            data_dir=data_dir,
            data_files=data_files,
            download_config=download_config,
            download_mode=download_mode,
        ).get_module()
    # Try locally
    elif path.endswith(filename):
        raise RuntimeError(f"Dataset scripts are no longer supported, but found {filename}")
    elif os.path.isfile(combined_path):
        raise RuntimeError(f"Dataset scripts are no longer supported, but found {filename}")
    elif os.path.isdir(path):
        return LocalDatasetModuleFactory(
            path, data_dir=data_dir, data_files=data_files, download_mode=download_mode
        ).get_module()
    # Try remotely
    elif is_relative_path(path) and path.count("/") <= 1:
        try:
            # Get the Dataset Card + get the revision + check authentication all at in one call
            # We fix the commit_hash in case there are new commits in the meantime
            api = HfApi(
                endpoint=config.HF_ENDPOINT,
                token=download_config.token,
                library_name="datasets",
                library_version=__version__,
                user_agent=get_datasets_user_agent(download_config.user_agent),
            )
            try:
                _raise_if_offline_mode_is_enabled()
                dataset_readme_path = api.hf_hub_download(
                    repo_id=path,
                    filename=config.REPOCARD_FILENAME,
                    repo_type="dataset",
                    revision=revision,
                    proxies=download_config.proxies,
                )
                commit_hash = os.path.basename(os.path.dirname(dataset_readme_path))
            except LocalEntryNotFoundError as e:
                if isinstance(
                    e.__cause__,
                    (
                        OfflineModeIsEnabled,
                        requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                    ),
                ):
                    raise ConnectionError(f"Couldn't reach '{path}' on the Hub ({e.__class__.__name__})") from e
                else:
                    raise
            except EntryNotFoundError:
                commit_hash = api.dataset_info(
                    path,
                    revision=revision,
                    timeout=100.0,
                ).sha
            except (
                OfflineModeIsEnabled,
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as e:
                raise ConnectionError(f"Couldn't reach '{path}' on the Hub ({e.__class__.__name__})") from e
            except GatedRepoError as e:
                message = f"Dataset '{path}' is a gated dataset on the Hub."
                if e.response.status_code == 401:
                    message += " You must be authenticated to access it."
                elif e.response.status_code == 403:
                    message += f" Visit the dataset page at https://huggingface.co/datasets/{path} to ask for access."
                raise DatasetNotFoundError(message) from e
            except RevisionNotFoundError as e:
                raise DatasetNotFoundError(
                    f"Revision '{revision}' doesn't exist for dataset '{path}' on the Hub."
                ) from e
            except RepositoryNotFoundError as e:
                raise DatasetNotFoundError(f"Dataset '{path}' doesn't exist on the Hub or cannot be accessed.") from e
            try:
                api.hf_hub_download(
                    repo_id=path,
                    filename=filename,
                    repo_type="dataset",
                    revision=commit_hash,
                    proxies=download_config.proxies,
                )
                raise RuntimeError(f"Dataset scripts are no longer supported, but found {filename}")
            except EntryNotFoundError:
                # Use the infos from the parquet export except in some cases:
                if data_dir or data_files or (revision and revision != "main"):
                    use_exported_dataset_infos = False
                else:
                    use_exported_dataset_infos = True
                return HubDatasetModuleFactory(
                    path,
                    commit_hash=commit_hash,
                    data_dir=data_dir,
                    data_files=data_files,
                    download_config=download_config,
                    download_mode=download_mode,
                    use_exported_dataset_infos=use_exported_dataset_infos,
                ).get_module()
            except GatedRepoError as e:
                message = f"Dataset '{path}' is a gated dataset on the Hub."
                if e.response.status_code == 401:
                    message += " You must be authenticated to access it."
                elif e.response.status_code == 403:
                    message += f" Visit the dataset page at https://huggingface.co/datasets/{path} to ask for access."
                raise DatasetNotFoundError(message) from e
            except RevisionNotFoundError as e:
                raise DatasetNotFoundError(
                    f"Revision '{revision}' doesn't exist for dataset '{path}' on the Hub."
                ) from e
        except Exception as e1:
            # All the attempts failed, before raising the error we should check if the module is already cached
            try:
                return CachedDatasetModuleFactory(path, cache_dir=cache_dir).get_module()
            except Exception:
                # If it's not in the cache, then it doesn't exist.
                if isinstance(e1, OfflineModeIsEnabled):
                    raise ConnectionError(f"Couldn't reach the Hugging Face Hub for dataset '{path}': {e1}") from None
                if isinstance(e1, (DataFilesNotFoundError, DatasetNotFoundError, EmptyDatasetError)):
                    raise e1 from None
                if isinstance(e1, FileNotFoundError):
                    raise FileNotFoundError(
                        f"Couldn't find any data file at {relative_to_absolute_path(path)}. "
                        f"Couldn't find '{path}' on the Hugging Face Hub either: {type(e1).__name__}: {e1}"
                    ) from None
                raise e1 from None
    else:
        raise FileNotFoundError(f"Couldn't find any data file at {relative_to_absolute_path(path)}.")


def load_dataset_builder(
    path: str,
    name: Optional[str] = None,
    data_dir: Optional[str] = None,
    data_files: Optional[Union[str, Sequence[str], Mapping[str, Union[str, Sequence[str]]]]] = None,
    cache_dir: Optional[str] = None,
    features: Optional[Features] = None,
    download_config: Optional[DownloadConfig] = None,
    download_mode: Optional[Union[DownloadMode, str]] = None,
    revision: Optional[Union[str, Version]] = None,
    token: Optional[Union[bool, str]] = None,
    storage_options: Optional[dict] = None,
    **config_kwargs,
) -> DatasetBuilder:
    """Load a dataset builder which can be used to:

    - Inspect general information that is required to build a dataset (cache directory, config, dataset info, features, data files, etc.)
    - Download and prepare the dataset as Arrow files in the cache
    - Get a streaming dataset without downloading or caching anything

    You can find the list of datasets on the [Hub](https://huggingface.co/datasets) or with [`huggingface_hub.list_datasets`].

    A dataset is a directory that contains some data files in generic formats (JSON, CSV, Parquet, etc.) and possibly
    in a generic structure (Webdataset, ImageFolder, AudioFolder, VideoFolder, etc.)

    Args:

        path (`str`):
            Path or name of the dataset.

            - if `path` is a dataset repository on the HF hub (list all available datasets with [`huggingface_hub.list_datasets`])
              -> load the dataset builder from supported files in the repository (csv, json, parquet, etc.)
              e.g. `'username/dataset_name'`, a dataset repository on the HF hub containing the data files.

            - if `path` is a local directory
              -> load the dataset builder from supported files in the directory (csv, json, parquet, etc.)
              e.g. `'./path/to/directory/with/my/csv/data'`.

            - if `path` is the name of a dataset builder and `data_files` or `data_dir` is specified
              (available builders are "json", "csv", "parquet", "arrow", "text", "xml", "webdataset", "imagefolder", "audiofolder", "videofolder")
              -> load the dataset builder from the files in `data_files` or `data_dir`
              e.g. `'parquet'`.

        name (`str`, *optional*):
            Defining the name of the dataset configuration.
        data_dir (`str`, *optional*):
            Defining the `data_dir` of the dataset configuration. If specified for the generic builders (csv, text etc.) or the Hub datasets and `data_files` is `None`,
            the behavior is equal to passing `os.path.join(data_dir, **)` as `data_files` to reference all the files in a directory.
        data_files (`str` or `Sequence` or `Mapping`, *optional*):
            Path(s) to source data file(s).
        cache_dir (`str`, *optional*):
            Directory to read/write data. Defaults to `"~/.cache/huggingface/datasets"`.
        features ([`Features`], *optional*):
            Set the features type to use for this dataset.
        download_config ([`DownloadConfig`], *optional*):
            Specific download configuration parameters.
        download_mode ([`DownloadMode`] or `str`, defaults to `REUSE_DATASET_IF_EXISTS`):
            Download/generate mode.
        revision ([`Version`] or `str`, *optional*):
            Version of the dataset to load.
            As datasets have their own git repository on the Datasets Hub, the default version "main" corresponds to their "main" branch.
            You can specify a different version than the default "main" by using a commit SHA or a git tag of the dataset repository.
        token (`str` or `bool`, *optional*):
            Optional string or boolean to use as Bearer token for remote files on the Datasets Hub.
            If `True`, or not specified, will get token from `"~/.huggingface"`.
        storage_options (`dict`, *optional*, defaults to `None`):
            **Experimental**. Key/value pairs to be passed on to the dataset file-system backend, if any.

            <Added version="2.11.0"/>

        **config_kwargs (additional keyword arguments):
            Keyword arguments to be passed to the [`BuilderConfig`]
            and used in the [`DatasetBuilder`].

    Returns:
        [`DatasetBuilder`]

    Example:

    ```py
    >>> from datasets import load_dataset_builder
    >>> ds_builder = load_dataset_builder('cornell-movie-review-data/rotten_tomatoes')
    >>> ds_builder.info.features
    {'label': ClassLabel(names=['neg', 'pos'], id=None),
     'text': Value(dtype='string', id=None)}
    ```
    """
    download_mode = DownloadMode(download_mode or DownloadMode.REUSE_DATASET_IF_EXISTS)
    if token is not None:
        download_config = download_config.copy() if download_config else DownloadConfig()
        download_config.token = token
    if storage_options is not None:
        download_config = download_config.copy() if download_config else DownloadConfig()
        download_config.storage_options.update(storage_options)
    dataset_module = dataset_module_factory(
        path,
        revision=revision,
        download_config=download_config,
        download_mode=download_mode,
        data_dir=data_dir,
        data_files=data_files,
        cache_dir=cache_dir,
    )
    # Get dataset builder class
    builder_kwargs = dataset_module.builder_kwargs
    data_dir = builder_kwargs.pop("data_dir", data_dir)
    data_files = builder_kwargs.pop("data_files", data_files)
    config_name = builder_kwargs.pop(
        "config_name", name or dataset_module.builder_configs_parameters.default_config_name
    )
    dataset_name = builder_kwargs.pop("dataset_name", None)
    info = dataset_module.dataset_infos.get(config_name) if dataset_module.dataset_infos else None

    if (
        path in _PACKAGED_DATASETS_MODULES
        and data_files is None
        and dataset_module.builder_configs_parameters.builder_configs[0].data_files is None
    ):
        error_msg = f"Please specify the data files or data directory to load for the {path} dataset builder."
        example_extensions = [
            extension for extension in _EXTENSION_TO_MODULE if _EXTENSION_TO_MODULE[extension] == path
        ]
        if example_extensions:
            error_msg += f'\nFor example `data_files={{"train": "path/to/data/train/*.{example_extensions[0]}"}}`'
        raise ValueError(error_msg)

    builder_cls = get_dataset_builder_class(dataset_module, dataset_name=dataset_name)
    # Instantiate the dataset builder
    builder_instance: DatasetBuilder = builder_cls(
        cache_dir=cache_dir,
        dataset_name=dataset_name,
        config_name=config_name,
        data_dir=data_dir,
        data_files=data_files,
        hash=dataset_module.hash,
        info=info,
        features=features,
        token=token,
        storage_options=storage_options,
        **builder_kwargs,
        **config_kwargs,
    )
    builder_instance._use_legacy_cache_dir_if_possible(dataset_module)

    return builder_instance


def load_dataset(
    path: str,
    name: Optional[str] = None,
    data_dir: Optional[str] = None,
    data_files: Optional[Union[str, Sequence[str], Mapping[str, Union[str, Sequence[str]]]]] = None,
    split: Optional[Union[str, Split, list[str], list[Split]]] = None,
    cache_dir: Optional[str] = None,
    features: Optional[Features] = None,
    download_config: Optional[DownloadConfig] = None,
    download_mode: Optional[Union[DownloadMode, str]] = None,
    verification_mode: Optional[Union[VerificationMode, str]] = None,
    keep_in_memory: Optional[bool] = None,
    save_infos: bool = False,
    revision: Optional[Union[str, Version]] = None,
    token: Optional[Union[bool, str]] = None,
    streaming: bool = False,
    num_proc: Optional[int] = None,
    storage_options: Optional[dict] = None,
    **config_kwargs,
) -> Union[DatasetDict, Dataset, IterableDatasetDict, IterableDataset]:
    """Load a dataset from the Hugging Face Hub, or a local dataset.

    You can find the list of datasets on the [Hub](https://huggingface.co/datasets) or with [`huggingface_hub.list_datasets`].

    A dataset is a directory that contains some data files in generic formats (JSON, CSV, Parquet, etc.) and possibly
    in a generic structure (Webdataset, ImageFolder, AudioFolder, VideoFolder, etc.)

    This function does the following under the hood:

        1. Load a dataset builder:

            * Find the most common data format in the dataset and pick its associated builder (JSON, CSV, Parquet, Webdataset, ImageFolder, AudioFolder, etc.)
            * Find which file goes into which split (e.g. train/test) based on file and directory names or on the YAML configuration
            * It is also possible to specify `data_files` manually, and which dataset builder to use (e.g. "parquet").

        2. Run the dataset builder:

            In the general case:

            * Download the data files from the dataset if they are not already available locally or cached.
            * Process and cache the dataset in typed Arrow tables for caching.

                Arrow table are arbitrarily long, typed tables which can store nested objects and be mapped to numpy/pandas/python generic types.
                They can be directly accessed from disk, loaded in RAM or even streamed over the web.

            In the streaming case:

            * Don't download or cache anything. Instead, the dataset is lazily loaded and will be streamed on-the-fly when iterating on it.

        3. Return a dataset built from the requested splits in `split` (default: all).

    Args:

        path (`str`):
            Path or name of the dataset.

            - if `path` is a dataset repository on the HF hub (list all available datasets with [`huggingface_hub.list_datasets`])
              -> load the dataset from supported files in the repository (csv, json, parquet, etc.)
              e.g. `'username/dataset_name'`, a dataset repository on the HF hub containing the data files.

            - if `path` is a local directory
              -> load the dataset from supported files in the directory (csv, json, parquet, etc.)
              e.g. `'./path/to/directory/with/my/csv/data'`.

            - if `path` is the name of a dataset builder and `data_files` or `data_dir` is specified
              (available builders are "json", "csv", "parquet", "arrow", "text", "xml", "webdataset", "imagefolder", "audiofolder", "videofolder")
              -> load the dataset from the files in `data_files` or `data_dir`
              e.g. `'parquet'`.

        name (`str`, *optional*):
            Defining the name of the dataset configuration.
        data_dir (`str`, *optional*):
            Defining the `data_dir` of the dataset configuration. If specified for the generic builders (csv, text etc.) or the Hub datasets and `data_files` is `None`,
            the behavior is equal to passing `os.path.join(data_dir, **)` as `data_files` to reference all the files in a directory.
        data_files (`str` or `Sequence` or `Mapping`, *optional*):
            Path(s) to source data file(s).
        split (`Split` or `str`):
            Which split of the data to load.
            If `None`, will return a `dict` with all splits (typically `datasets.Split.TRAIN` and `datasets.Split.TEST`).
            If given, will return a single Dataset.
            Splits can be combined and specified like in tensorflow-datasets.
        cache_dir (`str`, *optional*):
            Directory to read/write data. Defaults to `"~/.cache/huggingface/datasets"`.
        features (`Features`, *optional*):
            Set the features type to use for this dataset.
        download_config ([`DownloadConfig`], *optional*):
            Specific download configuration parameters.
        download_mode ([`DownloadMode`] or `str`, defaults to `REUSE_DATASET_IF_EXISTS`):
            Download/generate mode.
        verification_mode ([`VerificationMode`] or `str`, defaults to `BASIC_CHECKS`):
            Verification mode determining the checks to run on the downloaded/processed dataset information (checksums/size/splits/...).

            <Added version="2.9.1"/>
        keep_in_memory (`bool`, defaults to `None`):
            Whether to copy the dataset in-memory. If `None`, the dataset
            will not be copied in-memory unless explicitly enabled by setting `datasets.config.IN_MEMORY_MAX_SIZE` to
            nonzero. See more details in the [improve performance](../cache#improve-performance) section.
        save_infos (`bool`, defaults to `False`):
            Save the dataset information (checksums/size/splits/...).
        revision ([`Version`] or `str`, *optional*):
            Version of the dataset to load.
            As datasets have their own git repository on the Datasets Hub, the default version "main" corresponds to their "main" branch.
            You can specify a different version than the default "main" by using a commit SHA or a git tag of the dataset repository.
        token (`str` or `bool`, *optional*):
            Optional string or boolean to use as Bearer token for remote files on the Datasets Hub.
            If `True`, or not specified, will get token from `"~/.huggingface"`.
        streaming (`bool`, defaults to `False`):
            If set to `True`, don't download the data files. Instead, it streams the data progressively while
            iterating on the dataset. An [`IterableDataset`] or [`IterableDatasetDict`] is returned instead in this case.

            Note that streaming works for datasets that use data formats that support being iterated over like txt, csv, jsonl for example.
            Json files may be downloaded completely. Also streaming from remote zip or gzip files is supported but other compressed formats
            like rar and xz are not yet supported. The tgz format doesn't allow streaming.
        num_proc (`int`, *optional*, defaults to `None`):
            Number of processes when downloading and generating the dataset locally.
            Multiprocessing is disabled by default.

            <Added version="2.7.0"/>
        storage_options (`dict`, *optional*, defaults to `None`):
            **Experimental**. Key/value pairs to be passed on to the dataset file-system backend, if any.

            <Added version="2.11.0"/>
        **config_kwargs (additional keyword arguments):
            Keyword arguments to be passed to the `BuilderConfig`
            and used in the [`DatasetBuilder`].

    Returns:
        [`Dataset`] or [`DatasetDict`]:
        - if `split` is not `None`: the dataset requested,
        - if `split` is `None`, a [`~datasets.DatasetDict`] with each split.

        or [`IterableDataset`] or [`IterableDatasetDict`]: if `streaming=True`

        - if `split` is not `None`, the dataset is requested
        - if `split` is `None`, a [`~datasets.streaming.IterableDatasetDict`] with each split.

    Example:

    Load a dataset from the Hugging Face Hub:

    ```py
    >>> from datasets import load_dataset
    >>> ds = load_dataset('cornell-movie-review-data/rotten_tomatoes', split='train')

    # Load a subset or dataset configuration (here 'sst2')
    >>> from datasets import load_dataset
    >>> ds = load_dataset('nyu-mll/glue', 'sst2', split='train')

    # Manual mapping of data files to splits
    >>> data_files = {'train': 'train.csv', 'test': 'test.csv'}
    >>> ds = load_dataset('namespace/your_dataset_name', data_files=data_files)

    # Manual selection of a directory to load
    >>> ds = load_dataset('namespace/your_dataset_name', data_dir='folder_name')
    ```

    Load a local dataset:

    ```py
    # Load a CSV file
    >>> from datasets import load_dataset
    >>> ds = load_dataset('csv', data_files='path/to/local/my_dataset.csv')

    # Load a JSON file
    >>> from datasets import load_dataset
    >>> ds = load_dataset('json', data_files='path/to/local/my_dataset.json')
    ```

    Load an [`~datasets.IterableDataset`]:

    ```py
    >>> from datasets import load_dataset
    >>> ds = load_dataset('cornell-movie-review-data/rotten_tomatoes', split='train', streaming=True)
    ```

    Load an image dataset with the `ImageFolder` dataset builder:

    ```py
    >>> from datasets import load_dataset
    >>> ds = load_dataset('imagefolder', data_dir='/path/to/images', split='train')
    ```
    """
    if "trust_remote_code" in config_kwargs:
        if config_kwargs.pop("trust_remote_code"):
            logger.error(
                "`trust_remote_code` is not supported anymore.\n"
                f"Please check that the Hugging Face dataset '{path}' isn't based on a loading script and remove `trust_remote_code`.\n"
                "If the dataset is based on a loading script, please ask the dataset author to remove it and convert it to a standard format like Parquet."
            )
    if data_files is not None and not data_files:
        raise ValueError(f"Empty 'data_files': '{data_files}'. It should be either non-empty or None (default).")
    if Path(path, config.DATASET_STATE_JSON_FILENAME).exists():
        raise ValueError(
            "You are trying to load a dataset that was saved using `save_to_disk`. "
            "Please use `load_from_disk` instead."
        )

    if streaming and num_proc is not None:
        raise NotImplementedError(
            "Loading a streaming dataset in parallel with `num_proc` is not implemented. "
            "To parallelize streaming, you can wrap the dataset with a PyTorch DataLoader using `num_workers` > 1 instead."
        )

    download_mode = DownloadMode(download_mode or DownloadMode.REUSE_DATASET_IF_EXISTS)
    verification_mode = VerificationMode(
        (verification_mode or VerificationMode.BASIC_CHECKS) if not save_infos else VerificationMode.ALL_CHECKS
    )

    # Create a dataset builder
    builder_instance = load_dataset_builder(
        path=path,
        name=name,
        data_dir=data_dir,
        data_files=data_files,
        cache_dir=cache_dir,
        features=features,
        download_config=download_config,
        download_mode=download_mode,
        revision=revision,
        token=token,
        storage_options=storage_options,
        **config_kwargs,
    )

    # Return iterable dataset in case of streaming
    if streaming:
        return builder_instance.as_streaming_dataset(split=split)

    # Download and prepare data
    builder_instance.download_and_prepare(
        download_config=download_config,
        download_mode=download_mode,
        verification_mode=verification_mode,
        num_proc=num_proc,
        storage_options=storage_options,
    )

    # Build dataset for splits
    keep_in_memory = (
        keep_in_memory if keep_in_memory is not None else is_small_dataset(builder_instance.info.dataset_size)
    )
    ds = builder_instance.as_dataset(split=split, verification_mode=verification_mode, in_memory=keep_in_memory)
    if save_infos:
        builder_instance._save_infos()

    return ds


def load_from_disk(
    dataset_path: PathLike, keep_in_memory: Optional[bool] = None, storage_options: Optional[dict] = None
) -> Union[Dataset, DatasetDict]:
    """
    Loads a dataset that was previously saved using [`~Dataset.save_to_disk`] from a dataset directory, or
    from a filesystem using any implementation of `fsspec.spec.AbstractFileSystem`.

    Args:
        dataset_path (`path-like`):
            Path (e.g. `"dataset/train"`) or remote URI (e.g. `"s3://my-bucket/dataset/train"`)
            of the [`Dataset`] or [`DatasetDict`] directory where the dataset/dataset-dict will be
            loaded from.
        keep_in_memory (`bool`, defaults to `None`):
            Whether to copy the dataset in-memory. If `None`, the dataset
            will not be copied in-memory unless explicitly enabled by setting `datasets.config.IN_MEMORY_MAX_SIZE` to
            nonzero. See more details in the [improve performance](../cache#improve-performance) section.

        storage_options (`dict`, *optional*):
            Key/value pairs to be passed on to the file-system backend, if any.

            <Added version="2.9.0"/>

    Returns:
        [`Dataset`] or [`DatasetDict`]:
        - If `dataset_path` is a path of a dataset directory: the dataset requested.
        - If `dataset_path` is a path of a dataset dict directory, a [`DatasetDict`] with each split.

    Example:

    ```py
    >>> from datasets import load_from_disk
    >>> ds = load_from_disk('path/to/dataset/directory')
    ```
    """
    fs: fsspec.AbstractFileSystem
    fs, *_ = url_to_fs(dataset_path, **(storage_options or {}))
    if not fs.exists(dataset_path):
        raise FileNotFoundError(f"Directory {dataset_path} not found")
    if fs.isfile(posixpath.join(dataset_path, config.DATASET_INFO_FILENAME)) and fs.isfile(
        posixpath.join(dataset_path, config.DATASET_STATE_JSON_FILENAME)
    ):
        return Dataset.load_from_disk(dataset_path, keep_in_memory=keep_in_memory, storage_options=storage_options)
    elif fs.isfile(posixpath.join(dataset_path, config.DATASETDICT_JSON_FILENAME)):
        return DatasetDict.load_from_disk(dataset_path, keep_in_memory=keep_in_memory, storage_options=storage_options)
    else:
        raise FileNotFoundError(
            f"Directory {dataset_path} is neither a `Dataset` directory nor a `DatasetDict` directory."
        )
