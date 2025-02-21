from typing import List

from dataclassy import dataclass

from boiling_learning.preprocessing import ExperimentVideo, ImageDataset
from boiling_learning.utils.utils import (
    PathLike,
    ensure_resolved,
    print_header,
    print_verbose,
)


@dataclass(frozen=True)
class Options:
    convert_videos: bool = False
    extract_audios: bool = False
    pre_load_videos: bool = False
    extract_frames: bool = False


def main(
    datapath: PathLike, options: Options, verbose: bool = True
) -> List[ImageDataset]:
    datapath = ensure_resolved(datapath)

    datasets: List[ImageDataset] = []
    for casedir in datapath.iterdir():
        if not casedir.is_dir():
            continue

        case = casedir.name
        for subcasedir in casedir.iterdir():
            if not subcasedir.is_dir():
                continue

            subcase = subcasedir.name

            dataset: ImageDataset = ImageDataset(f'{case}:{subcase}')
            for testdir in subcasedir.iterdir():
                test_name = testdir.name

                videopaths = (testdir / 'videos').glob('*.mp4')
                for video_path in videopaths:
                    video_name = video_path.stem
                    ev_name = ':'.join((case, subcase, test_name, video_name))
                    ev = ExperimentVideo(
                        df_path=video_path.with_suffix('.csv'),
                        video_path=video_path,
                        name=ev_name,
                    )
                    dataset.add(ev)

            if verbose:
                print_header(dataset.name)

            if options.extract_audios:
                print_verbose(verbose, 'Extracting audios')
                dataset.extract_audios(verbose=True)
            if options.pre_load_videos:
                print_verbose(verbose, 'Opening videos')
                dataset.open_videos()
            if options.extract_frames:
                print_verbose(verbose, 'Extracting videos')
                dataset.extract_frames(
                    overwrite=False,
                    verbose=2,
                    chunk_sizes=(100, 100),
                    iterate=True,
                )

            datasets.append(dataset)
    return datasets


if __name__ == '__main__':
    raise RuntimeError(
        '*load_dataset_tree* cannot be executed as a standalone script yet.'
    )
